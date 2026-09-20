# 桌面版系统托盘（后台运行）可行性研究

> 研究范围：`desktop.py` + `packaging/` + `backend/lifecycle.py` / `shutdown.py` 这条桌面链路。
> 方法：读代码 + 在**本机真实桌面会话**（KDE / Wayland，会话总线可用）上做原型实测。
> 文中每条结论都标注了「实测」或「仅文档/未验证」，不混为一谈。

---

## 0. 结论（TL;DR）

> **实施状态（2026-09-20）**：三个平台**均已实现**——`backend/tray.py`（统一入口）+
> `backend/tray_gtk.py` / `tray_win.py` / `tray_mac.py` + `desktop.py` 的关窗语义 +
> `frontend/tray.png` + `--tray/--no-tray`。
> 阶段一（Linux）**已实机验证**：真实桌面应用启动后托盘项注册进宿主（7 → 8 项），
> Quit 后随进程消失（回到 7）、退出码 0。
> 阶段二/三（Windows/macOS）在源码层面完成并验证：pywebview 三平台后端的关窗否决
> 与 hide/show 逐行读过源码（见 §3.2/§3.3），pystray 0.19.5 的 `run_detached`/
> `stop`/跨线程 `title` 也逐行读过；接线契约（菜单顺序、selector 名、主线程 marshal、
> target 保留、ready 超时）用假模块测试钉住（`tests/test_tray.py`，三平台共 29 项）。
> **托盘图标是否真的可见、菜单点击、关窗后的隐藏与恢复，仍需 Windows/macOS 真机确认。**

**可行。** 三平台都能做到"关窗不退出、留在托盘、随时回来"，而且：

- **Linux 已在本机实测通过**：托盘图标真的注册进了 KDE 的 `StatusNotifierWatcher`（细节见 §3.1）；
- **macOS 大概率零新增依赖**：pywebview 在 macOS 上本来就依赖 pyobjc（Cocoa），`NSStatusItem` 直接可用；
- **Windows 需要一个托盘库或一小段 ctypes**，且 pywebview 的窗口后端本来就有 `hide/show`；
- **pywebview 6.2.1 自己没有任何托盘 API**，所以托盘必须我们接（这是本次研究最硬的一个约束）。

但先要厘清一件事：**"后台运行"今天已经成立了 80%**。OCR 跑在服务器进程的守护线程上，
窗口只是一个视图——最小化就已经在后台跑，`--no-window` 也能无窗口服务。
真正缺的只有两件：**① 关窗不等于退出；② 关窗之后有一个能回来的入口（托盘）**。

---

## 1. 现状：三条链路（读代码得到的事实）

| 链路 | 位置 | 现状 |
| --- | --- | --- |
| 窗口 | `desktop.py:_create_window` / `_watch_for_quit` | pywebview 窗口；GTK（Linux）/ winforms（Windows）/ cocoa（macOS） |
| 服务器 | `backend/server.py::EmbeddedServer` | 后台线程 + 内核分配端口；**与窗口无关** |
| 退出 | `backend/lifecycle.py` + `backend/shutdown.py` | 所有入口（关窗／UI Quit／Ctrl-C／SIGTERM）都收敛到 `request_quit` → shutdown hook → 有界停机 |

关键事实：**关窗就是退出**（`desktop.py:199-201` 里 `closing` 处理器直接 `request_quit`），
退出会把正在跑的 OCR 优雅取消（取消标志 + 有界等待）。所以"关掉窗口让它继续跑"目前做不到。

### 1.1 顺带发现一处**注释与实现相反**（改托盘时一定会踩）

`desktop.py:196-198` 写着：

> The handler must return True: pywebview CANCELS a close whose handler returned a falsy value.

实测（pywebview 6.2.1 源码）：

- `webview/event.py::Event.set()` 的返回值是
  `len([v for v in return_values if v is False]) != 0` —— **只有返回字面量 `False` 才算"取消"**，
  不是"假值"（`None`/`0`/`""` 都不算）；
- `webview/platforms/gtk.py::close_window()`：`should_cancel = events.closing.set()`，
  然后 `if should_cancel: return True` —— GTK 收到 True 表示**取消删除事件**，窗口留下。

也就是说：

| 处理器返回 | 效果 |
| --- | --- |
| `False` | **取消关闭**（窗口不动）—— 这正是"关窗进托盘"需要的 |
| `True` / `None` / 其它 | 允许关闭，窗口销毁，GUI 循环结束 |

现在的代码返回 `True`，行为**是对的**（允许关闭 → 窗口销毁 → `webview.start` 返回 → teardown），
但注释把语义说反了。实现托盘时必须改成"返回 `False` + `window.hide()`"，届时这条注释必须一起改掉，
否则下一个人会照着注释写出"窗口关不掉"的 bug。

另外两个利好（实测）：

- `closing` 事件是**同步**执行（`Event(self, True)` → `should_lock=True`），处理器里直接调
  `window.hide()` 是安全的；
- GTK 后端的 `hide()/show()` 内部是 `glib.idle_add(...)`，**从托盘回调线程调用也安全**。

---

## 2. "后台运行"到底缺什么

| 能力 | 今天 | 备注 |
| --- | --- | --- |
| 最小化时继续 OCR | ✅ | 服务器独立于窗口 |
| 关窗时继续 OCR | ❌ | 关窗 = 退出（会取消任务） |
| 完全无窗口运行 | ✅ | `--no-window`（但没有回窗口的入口） |
| 无窗口时看进度 | ⚠️ | 得自己记住 URL / 开浏览器 |
| 托盘图标 / 菜单 | ❌ | pywebview 无此 API |
| 隐藏后有办法回来 | ❌ | 无托盘时不可做——这会变成"窗口找不回来"的陷阱 |

**设计红线**：**只有托盘真的建起来了，才可以改变"关窗=退出"的行为。**
建不起来就维持现状（否则用户会有一个藏起来又没有任何入口的窗口）。

---

## 3. 托盘技术路线（含实测）

### 3.1 Linux（本机实测：可用）

环境：KDE / Wayland，`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`，
`org.kde.StatusNotifierWatcher` 在线且 `IsStatusNotifierHostRegistered: True`。

实测方法（可复现）：读 watcher 的 `RegisteredStatusNotifierItems` 属性 → 创建指示器 → 再读一次：

```
before: 7 item(s)
[AyatanaAppIndicator3] indicator created (pid 6)
during: 8 item(s)
NEW items registered by our process: [':1.5932/org/ayatana/NotificationItem/pdf_ocr_embed_probe']
```

`AppIndicator3`（旧名）与 `AyatanaAppIndicator3`（维护分支）**都成功**。本机两套 typelib 和共享库都在。

注意点：

- **`Gtk.StatusIcon` 不要用**：它是 XEmbed 时代的东西（GTK 3.14 起弃用、GTK4 移除），
  **Wayland 下根本不显示**；
- 库会打印 `libayatana-appindicator is deprecated. Please use libayatana-appindicator-glib`；
  但**能用**，且 `libayatana-appindicator-glib` 本机并未安装；
- **GNOME 默认没有托盘**（需要 AppIndicator 扩展）→ 这时"注册成功"也不等于"看得见"（见 §7 风险）；
- 另一条可选路线是**直连 StatusNotifierItem 协议**（`Gio.DBus` 自己注册 SNI + 实现
  `com.canonical.dbusmenu`）：零系统依赖、理论最稳，但要自己写 ~200 行菜单协议，建议只在不想依赖
  libayatana 时才走。

### 3.2 Windows（已实现：pystray；源码层面验证）

> ✅ **2026-09-20 更新：已用 pystray 实现**（`backend/tray_win.py`），依赖标记
> `sys_platform == "win32"` 进 `requirements-desktop.txt`，spec 里按需收集
> `collect_submodules("pystray")`（动态导入，静态分析看不到）。

pywebview 6.2.1 的 winforms 后端（逐行读过源码，与 Linux 完全同构）：

- `on_closing`：`should_cancel = self.closing.set(); if should_cancel: args.Cancel = True`
  —— **同样的字面量 `False` 否决**，`desktop.py` 的关窗逻辑不用改；
- `hide()`/`show()` 内部 `self.Invoke(...)` marshal 到 UI 线程——从托盘线程调用安全。

pystray 0.19.5（逐行读过源码，实现里要遵守的契约）：

- `run_detached(setup)` = `threading.Thread(target=self._run).start()`——Win32 消息循环
  跑在**独立线程**；`setup` 回调在消息窗口就绪后才触发（`_mark_ready()` 之后）；
- **自定义 setup 必须自己设 `visible = True`**——默认 setup 只在不传 setup 时生效；
- 若循环线程在就绪前死掉，`setup` 永远不会跑 → 必须 ready-wait + 超时，超时即"没有托盘"；
- `stop()` 发 `WM_STOP` 后 join setup 线程（上限 `SETUP_THREAD_TIMEOUT = 5s`）；
- `title` 更新 = `Shell_NotifyIcon(NIM_MODIFY, NIF_TIP)`——全局 API，跨线程安全；
- `notify()` = `NIF_INFO` 气泡（Windows 10+ 自动转 toast）；`HAS_NOTIFICATION = True`；
- 图标经 `serialized_image(..., 'ICO')` + `LR_DEFAULTSIZE` 由 Windows 定尺寸，
  64×64 的 PNG 直接可用（官方示例就是 64×64）；
- **左键激活标了 `default` 的菜单项** → "显示主窗口"既是首项又是点击动作。

### 3.3 macOS（已实现：pyobjc NSStatusItem；源码层面验证）

> ✅ **2026-09-20 更新：已用 pyobjc `NSStatusItem` 实现**（`backend/tray_mac.py`），
> 零新增依赖——pywebview 在 macOS 本来就依赖 pyobjc。

pywebview 6.2.1 的 cocoa 后端（逐行读过源码，与 Linux 完全同构）：

- `should_close`：`should_cancel = window.events.closing.set(); if should_cancel: return Foundation.NO`
  —— **同样的字面量 `False` 否决**；
- `hide()`/`show()`（以及 `set_title`/`destroy`）全部经 `AppHelper.callAfter(...)`
  marshal 到主线程——**这就是我们要用的主线程 marshal 通道**，同一进程同一库。

pystray 在 macOS 确实不可用（逐行读过 `_darwin.py` 实锤）：

- `run_detached()` 只做 `self._mark_ready()`，**根本不跑循环**——托盘会"创建成功但永远
  没有事件循环"；它的循环必须在主线程跑 `run()`，与 pywebview 的主线程 Cocoa 循环冲突；
- 它的 `_notify` 用 `osascript`——我们自己的 `tray_mac.notify` 也用同一条通道
  （`subprocess.run(["osascript", "-e", ...])`，字符串做 AppleScript 转义，best-effort）。

`NSStatusItem` 实现要点：

- **每个 AppKit 调用都必须在主线程**——统一经 `AppHelper.callAfter(fn, *args)` marshal，
  需要结果时配 `threading.Event` + 超时（镜像 GTK 的 `idle_add` + wait 模式）；
- 菜单：`NSStatusItem.setMenu_(menu)` → 左键直接弹菜单；每项
  `NSMenuItem.initWithTitle_action_keyEquivalent_(标题, selector, "")` +
  `setTarget_(target)`，selector 是 pyobjc 自动映射的 `showWindow:` 等；
- **`NSMenuItem.target` 不被 AppKit retain**（unsafe_unretained）——target 对象必须
  在 Python 侧持有（`Tray` 实例保存引用），否则菜单点了没反应；
- tooltip/进度：`status_item.button().setTitle_` + `setToolTip_`（10.10+ 的
  `NSStatusBarButton`；更老系统退回 `setTitle_`）；
- 图标：`NSImage.initWithContentsOfFile_` + `setSize_((18, 18))`（菜单栏尺寸）；
- 退出时 `NSStatusBar.removeStatusItem_(item)`——必须在 `window.destroy()` 之前
  （`desktop._watch_for_quit` 的顺序已经保证），且 marshal 后**有界等待**完成。

### 3.4 小结对照表

| 平台 | 窗口 hide/show | 托盘实现 | 新增依赖 | 验证状态 |
| --- | --- | --- | --- | --- |
| Linux | ✅ gtk | `AyatanaAppIndicator3`（回退 `AppIndicator3`） | 无（PyGObject 已是桌面版硬依赖） | **本机实测可用** |
| Windows | ✅ winforms | pystray（`run_detached` 消息循环线程） | +1 包（`sys_platform=="win32"`，Pillow 已在树上） | 已实现；源码+假模块测试验证，真机待确认 |
| macOS | ✅ cocoa | pyobjc `NSStatusItem` | 通常 0 | 已实现；源码+假模块测试验证，真机待确认 |

---

## 4. 推荐设计

### 4.1 结构

```
backend/tray.py          # 统一入口：create_tray(...) -> TrayHandle | None
backend/tray_gtk.py      # Linux: AppIndicator（Ayatana -> AppIndicator 回退）
backend/tray_win.py      # Windows: pystray 或 ctypes（阶段二）
backend/tray_mac.py      # macOS: NSStatusItem（阶段二）
```

接口（关键：**任何失败都返回 None，绝不抛**）：

```python
def create_tray(*, title: str, icon_path: Path,
                on_show: Callable[[], None],
                on_open_browser: Callable[[], None],
                on_quit: Callable[[], None],
                status_provider: Callable[[], str]) -> object | None: ...
```

### 4.2 交互

- **关窗** → `closing` 处理器返回 `False`（取消关闭）+ `window.hide()`；
  **仅当托盘可用时**才这样（否则保持关窗=退出）。
- **托盘左键/菜单**：
  - `显示主窗口` → `window.show()`（GTK 已确认线程安全）
  - `在浏览器打开` → `webbrowser.open(server.url)`（无窗口/托盘失效时的兜底）
  - `退出` → `lifecycle.request_quit(timeout=QUIT_REQUEST_TIMEOUT)`，**完全复用现有有界退出**
- **托盘提示文字**（tooltip / 菜单首行）显示进度：直接调同进程的
  `ocr_service.list_jobs()`（无需 HTTP）→ 例如 `2 个任务运行中 · 第 187/224 页`；
  空闲时显示 `空闲`。这是"后台运行"真正的价值所在：不用开窗口就知道在干什么。
- **WebUI 里的 Quit 按钮**：仍然是**彻底退出**（用户明确说退出，就该退出）。

### 4.3 开关与默认值

新增 `--tray` / `--no-tray`（`desktop.py`）：

- `--no-tray`：永远不建托盘，关窗=退出（等价今天的行为，也是排障开关）；
- 默认（都不给）：**能建就建**（有显示会话 + 平台实现可用），建不起来就静默维持现状。

### 4.4 图标资产（现在缺）

实测：**仓库里没有任何图标文件**（`index.html` 的 favicon 是内联 SVG data URI；
`packaging/` 下没有 `.ico/.png/.icns`；spec 的 `datas` 只有 `frontend/` 和 `config.example.toml`，
EXE/COLLECT 也没有 `icon=`）。

要做的事：

1. 新增一份图标（建议 `frontend/` 下放 `tray.png` 64×64，另配 Windows `.ico` 与 macOS `.icns`）；
2. 打包时进 `datas`（`frontend/` 已经整个进包，所以 PNG 放那里**不用改 spec**；
   `.ico` 若要用于 EXE 图标则要加 `icon=`）；
3. 运行时用 `paths.resource_dir() / "frontend" / "tray.png"` 读取
   （**只读打包资源，绝不写**——遵守 AGENTS.md 的路径约定）。

---

## 5. 分阶段计划与成本

| 阶段 | 内容 | 成本 | 可验证性 |
| --- | --- | --- | --- |
| **一（Linux 可用）** | `backend/tray.py` + GTK 实现 + `desktop.py` 关窗语义 + `--tray/--no-tray` + 图标 + 进度提示 | **0.5–1 天** | ✅ **已完成**：注册可自动验证（§6），交互仍需人工确认 |
| **二（Windows）** | pystray 实现（或 ctypes），spec 的 hiddenimports | +0.5–1 天 | ✅ **已完成**（2026-09-20）：假模块测试钉住接线契约；真机交互待确认 |
| **三（macOS）** | `NSStatusItem` + accessory 策略打磨 | +0.5–1 天 | ✅ **已完成**（2026-09-20）：假模块测试钉住接线契约；真机交互待确认 |
| **可选** | 首次隐藏时发一条系统通知（✅ 已做：Linux libnotify / Windows 托盘气泡 / macOS osascript）；启动最小化到托盘；开机自启 | +0.5 天 | 人工 |

---

## 6. 测试与验证

**能自动化的（建议进 pytest）**

1. **把"关窗决策"抽成纯函数**并用单测覆盖，例如
   `desktop._should_hide_on_close(tray_available: bool, no_tray: bool) -> bool`：
   - 托盘可用且未禁用 → 隐藏（返回 False 取消关闭）
   - 托盘不可用 → 维持今天的行为（允许关闭）
2. **保住现有退出不变量**：`tests/test_exit_shutdown.py` 里 `desktop.py --no-window` 的子进程用例
   必须继续通过；再加一条"请求 `--tray` 但环境没有显示会话 → 仍然能干净退出"的用例。
3. `create_tray()` 在无托盘环境必须**返回 None 且不抛异常**。
4. 托盘注册可以自动断言（本机可用）：读 `RegisteredStatusNotifierItems` 前后差值——
   这正是本次研究用的方法；CI 里用 `pytest.mark.skipif(no session bus)` 跳过。

**必须人工的**：托盘图标是否真的可见（GNOME 无扩展时"注册成功但看不见"）、
菜单点击、关窗后的隐藏与恢复、隐藏状态下任务继续跑。

**现有测试的一个弱点（顺手该修）**：`tests/test_desktop.py::test_closing_the_window_requests_a_quit`
把处理器**内联复制**了一份（`_on_closing` 是 `_watch_for_quit` 内部的嵌套函数），
所以它断言的是测试自己的副本——改 `desktop.py` 的真实处理器它不会失败。
做托盘时应把它重构成调用真实函数（第 1 条的同一次改动）。

---

## 7. 风险与坑

1. **注册成功 ≠ 图标可见**（GNOME 无 AppIndicator 扩展时最容易中招）。缓解：
   首次隐藏时给一条通知 + 保留 WebUI 的 Quit + `--no-tray` 逃逸；文档里写清 GNOME 需要扩展。
2. **Wayland 下不能用 `Gtk.StatusIcon`**（XEmbed 不可见），必须走 SNI/AppIndicator。
3. **托盘线程与 GUI 主循环**：GTK 的 show/hide 已经是 `idle_add`，安全；但**不要在托盘回调里
   直接碰 GTK 控件**，只调 pywebview 的窗口 API。Windows 的 pystray 自带消息循环线程，
   也不要和 WinForms 循环混用。
4. **macOS 的 pystray 会撞主线程**——所以 macOS 走 pyobjc 而不是 pystray。
5. **打包体积/依赖**：Linux 实现零新增；Windows 若引入 pystray 只是一个小包（Pillow 已在树上）；
   macOS 通常零新增。注意 spec 的 `hiddenimports` 若是动态导入需要补。
6. **误以为"关窗=后台"就安全**：一旦托盘没起来，隐藏窗口会让用户以为程序没了。红线见 §4.2。
7. **研究过程中的一个方法论陷阱**（记下来避免复现）：`ListNames` **看不到** SNI 项
   （它们注册在自己的**唯一名**上，不是 well-known name），我第一次因此误判"legacy 库静默失效"；
   权威判据是 watcher 的 `RegisteredStatusNotifierItems` 属性，且 `Properties.Get` 的参数类型写错
   会返回错误而不是空列表——**错误必须当错误处理，不能当"没有"**。

---

## 8. 需要你决定的问题

1. **目标平台**：✅ 已定——先 Linux，后 Windows/macOS（现在三平台都已实现）。
2. **默认行为**：✅ 已定——**能建就建**，建不起来就维持"关窗即退出"。
3. **关窗 vs 退出的语义**：✅ 已定——关窗=隐藏，而 WebUI 的"退出"按钮=彻底退出。
4. **Windows 是否接受新增 `pystray` 依赖**：✅ 已定——**用 pystray**（纯 Python，
   Pillow 已在依赖树里；Win32 消息循环由久经考验的库处理，好过手写不可测试的 ctypes）。
5. **托盘要不要显示进度**：✅ 已定——要（tooltip 里 `第 187/224 页`）。
6. **要不要顺带做开机自启**（Linux: systemd user unit / autostart；Windows: 启动项；macOS: Login Item）——
   这是独立的一件事，可以单列。
