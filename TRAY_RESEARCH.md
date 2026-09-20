# 桌面版系统托盘（后台运行）可行性研究

> 研究范围：`desktop.py` + `packaging/` + `backend/lifecycle.py` / `shutdown.py` 这条桌面链路。
> 方法：读代码 + 在**本机真实桌面会话**（KDE / Wayland，会话总线可用）上做原型实测。
> 文中每条结论都标注了「实测」或「仅文档/未验证」，不混为一谈。

---

## 0. 结论（TL;DR）

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

### 3.2 Windows（仅文档，本机无法验证）

- 窗口后端是 `webview/platforms/winforms.py`，`create_window/hide/show` 都在（实测源码），
  所以"取消关闭 + 隐藏"在 pywebview 层面没问题；`edgechromium.py` 只是内嵌浏览器控件，不是窗口后端。
- 托盘两条路：
  1. **pystray**（新依赖，只有 `pystray` 本身；它需要的 Pillow 12.3.0 已经在依赖树里，
     因为 ocrmypdf 依赖 Pillow）——代码量最小；
  2. **ctypes 直接 `Shell_NotifyIcon`**——零新增依赖，但需要自己建 message-only 窗口 + WndProc，
     代码量大约 150-200 行。
- pywebview 在 Windows 上本来就依赖 `pythonnet`（实测 pywebview 元数据），所以进程里已有 .NET；
  但我们**不建议**把托盘塞进 WinForms 消息循环，独立线程 + 自己的消息循环更干净。

### 3.3 macOS（仅文档，本机无法验证）

- pywebview 在 macOS 上依赖 `pyobjc-core/Cocoa/Quartz/WebKit`（实测其 `Requires-Dist`），
  所以 **`NSStatusItem` 通常不需要新增依赖**。
- **不要用 pystray**：它在 macOS 需要占用**主线程**跑 Cocoa 事件循环，而 pywebview 的 Cocoa 循环
  已经在主线程 → 直接冲突（pystray 官方也说明 `run_detached` 在 macOS 不可用）。
  `NSStatusItem` 反而是最顺的：它挂在既有的 `NSApplication` run loop 上。
- 细节：窗口隐藏后 Dock 图标仍在；若要"纯菜单栏应用"，需要
  `NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)`，并在显示窗口时
  `activateIgnoringOtherApps_(True)`——属于打磨项。

### 3.4 小结对照表

| 平台 | 窗口 hide/show | 托盘实现 | 新增依赖 | 验证状态 |
| --- | --- | --- | --- | --- |
| Linux | ✅ gtk | `AyatanaAppIndicator3`（回退 `AppIndicator3`） | 无（PyGObject 已是桌面版硬依赖） | **本机实测可用** |
| Windows | ✅ winforms | pystray 或 ctypes Shell_NotifyIcon | 前者 +1 包 | 未验证（源码层面成立） |
| macOS | ✅ cocoa | pyobjc `NSStatusItem` | 通常 0 | 未验证（依赖已在树上） |

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
| **一（Linux 可用）** | `backend/tray.py` + GTK 实现 + `desktop.py` 关窗语义 + `--tray/--no-tray` + 图标 + 进度提示 | **0.5–1 天** | 本机可自动验证"图标已注册"（§6），交互人工确认 |
| **二（Windows）** | pystray 实现（或 ctypes），spec 的 hiddenimports | +0.5–1 天 | 需 Windows 真机 |
| **三（macOS）** | `NSStatusItem` + accessory 策略打磨 | +0.5–1 天 | 需 macOS 真机 |
| **可选** | 首次隐藏时发一条系统通知（`Notify` typelib 本机存在）；启动最小化到托盘；开机自启 | +0.5 天 | 人工 |

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

1. **目标平台**：只做 Linux，还是三平台都要？（决定是 0.5–1 天还是 2–3 天）
2. **默认行为**：装好即"关窗进托盘"，还是需要显式 `--tray`？我建议**能建就建**，但想听你的。
3. **关窗 vs 退出的语义**：关窗=隐藏（我的建议），而 WebUI 的"退出"按钮=彻底退出——同意吗？
4. **Windows 是否接受新增 `pystray` 依赖**（否则我改用 ctypes 自己写，代码多但零依赖）？
5. **托盘要不要显示进度**（tooltip 里 `第 187/224 页`）？我建议要，这是"后台运行"最有用的部分。
6. **要不要顺带做开机自启**（Linux: systemd user unit / autostart；Windows: 启动项；macOS: Login Item）——
   这是独立的一件事，可以单列。
