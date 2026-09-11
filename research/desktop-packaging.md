# 将 pdf-ocr-embed 打包为桌面 App：研究报告

> 目标形态：**Windows / macOS / Linux 可双击运行的桌面应用**（本地完成 OCR，无需用户自己装 Python）。
>
> 调研日期：2026-09。核验方式：阅读本仓库源码 + `.venv` 内实际安装的 `ocrmypdf 17.11.0` / `uvicorn` 源码 + 官方 changelog / PyPI / 文档联网检索。
> 凡未能从一手来源确认的结论，文中显式标注「**未核实**」。

---

## 0. 结论摘要

| 维度 | 结论 |
|---|---|
| **可行性** | ✅ 高。技术栈是「本机 HTTP 服务 + 零构建静态前端」，非常适合桌面封装；且**不依赖 CUDA/NVIDIA**，OCR 可完全本地化。 |
| **推荐工具链** | **PyInstaller（onedir）+ pywebview + platformdirs**；发布基线建议 **Python 3.13**（3.14 验证通过后再切）。 |
| **最大技术工作** | 把 6 处 `Path(__file__)` 相对路径改为「只读资源 / 可写用户目录」分离，并重写服务器启动方式（现在会**必然崩溃**，见 §6）。 |
| **最大法律风险** | ✅ **已消除**。原先最大的风险是 **PyMuPDF 的 AGPL-3.0**，现已按下文「路线 C」移除：渲染/文本抽取改用 `pypdfium2`（BSD/Apache），页面删除用 `pikepdf`（MPL-2.0）。两者都是 OCRmyPDF 的既有依赖，见 `backend/pdf_processing.py`。 |
| **一个重大利好** | OCRmyPDF 自 17.0.0 起 **Ghostscript 已非必需**（改用 `pypdfium2` 光栅化），且本项目默认 `output_type="pdf"`。**默认构建可以完全不带 Ghostscript**，少一个约 50MB 的 AGPL 外部二进制。 |
| **仍需捆绑的外部二进制** | `tesseract`（Apache-2.0）——仅当用户选择 Tesseract 本地引擎时需要；Unlimited API 引擎不需要。 |
| **预期体积** | 约 **180–330MB**（onedir；已移除 `pymupdf` 的约 65MB）。 |
| **当前许可证面** | `ocrmypdf`/`pikepdf` MPL-2.0、`pypdfium2` BSD/Apache、`tesseract` Apache-2.0、`fpdf2` LGPL-3.0（按库引入，保留声明即可）、其余 MIT/BSD —— **已无 AGPL 项**，可自由选择项目许可证（如 MIT/Apache-2.0）。 |

**一句话建议**：最大障碍（AGPL）已移除；技术上用 PyInstaller onedir + pywebview 即可落地，接下来直接进入 §7.2 的 P1。

---

## 1. 应用画像与打包难点

### 1.1 打包对象

| 层 | 内容 | 打包影响 |
|---|---|---|
| 后端 | FastAPI + uvicorn，本机 HTTP 服务 | 需禁用 reload/多 worker，改后台线程 + 随机端口 |
| 前端 | `frontend/` 原生 HTML/CSS/JS，**零构建、无 CDN**、全部用相对 URL | ✅ 打包友好，仅需作为数据文件收集 |
| OCR 核心 | `ocrmypdf 17.11.0`（MPL-2.0），**通过 subprocess 调用外部程序** | 需捆绑/定位 `tesseract`；`gs` 默认已不需要 |
| 插件引擎 | `ocrmypdf_unlimited/`（本项目源码目录），经 OCRmyPDF entry point / dotted module 加载 | 需 hidden import；entry point 元数据在冻结后行为要确认 |
| 原生轮子 | `pikepdf`(MPL，内含 libqpdf)、`pypdfium2`(BSD/Apache) —— **仅此两个** | hooks 可处理，但需实测 |
| 运行期行为 | 后台 `threading.Thread`、写 `uploads/ work/ output/`、长任务（分钟级） | 路径必须迁到用户目录；退出需优雅停止 |

### 1.2 难点排序

1. **路径**：`Path(__file__).resolve().parent.parent` 在冻结后指向只读/临时目录 → 上传、工作目录、配置保存、日志**全部失效**（§6.1）。
2. **启动方式**：`uvicorn.run(..., reload=True)` 在冻结后会让 exe **递归启动自己**（§6.2）。
3. ~~**许可证**：PyMuPDF 的 AGPL~~ ✅ **已解决**（§5.3 路线 C，PyMuPDF 已移除）。
4. **外部二进制定位**：`tesseract` 需随包提供或要求用户安装（§4）。
5. **entry point 插件发现**：冻结后 `importlib.metadata` 可能枚举不到插件（§6.4）。
6. **体积/冷启动**：onedir 优于 onefile（§2.2）。

---

## 2. 打包工具选型

### 2.1 对比

| 工具 | 最近版本（日期） | Python 3.14 | 平台 | 外部系统二进制 | 数据文件 |
|---|---|---|---|---|---|
| **PyInstaller** | 6.22.2（2026-08-17） | ✅ 自 **6.15.0**（2025-08-03） | Win/mac/Linux | ✅ `--add-binary`，可随包塞入任意可执行文件 | `--add-data` / `datas` / `copy_metadata()` |
| **Nuitka** | 4.2.1（2026-09-05） | ✅ 自 **4.2** 正式（2026-08-24）；2.8 实验 | Win/mac/Linux | 可用，配置较繁琐 | `--include-data-dir` |
| **cx_Freeze** | 8.7.0（2026-08-22） | ✅ 自 **8.5**（2025-11-24） | Win/mac/Linux | 可以，需手写 | `include_files` |
| **Briefcase** | 0.4.5（2026-09-08） | ✅ 自 **0.3.25**（2025-08-26） | Win/mac/Linux + 移动 | ❌ **只装 wheel，无通用机制塞入 tesseract/gs** | `sources`/`resources` |
| **py2exe** | 0.14.2.0 | ✅ | 仅 Windows | 可以 | `data_files` |
| **py2app** | 0.28.9+ | ✅（不支持 free-threaded） | 仅 macOS | 可以 | `resources` |
| **PyOxidizer** | 0.24.0（**2022-12-30**，已停更） | ❌ | 跨平台 | — | — |

来源：[PyInstaller CHANGES](https://pyinstaller.org/en/stable/CHANGES.html) · [Nuitka 4.2](https://nuitka.net/posts/nuitka-release-42.html) · [Briefcase releases](https://briefcase.beeware.org/en/latest/about/releases/) · [PyOxidizer](https://pypi.org/project/pyoxidizer/)

### 2.2 关键结论

- **PyInstaller 是本项目的首选**：它是主流跨平台冻结器里**唯一能自然地**把「外部系统可执行文件（tesseract）」连同 Python 依赖一起打成一个可双击目录的工具；Briefcase 在这点上是硬伤（它只接受 wheel）。
- **必须用 onedir，不要 onefile**：
  - onefile 每次启动都要把数百 MB 解压到临时目录，冷启动明显变慢；
  - PyInstaller 6.13.0 起**弃用 macOS `.app` + onefile 组合，v7.0 将禁止**，官方要求改用 onedir（[CHANGES](https://pyinstaller.org/en/stable/CHANGES.html)）。
  - 「双击体验」靠**安装器**（Windows Inno Setup/WiX、macOS `.dmg`、Linux AppImage/.deb）实现，而不是 onefile。
- **PyInstaller 不是交叉编译器**：必须在每个目标 OS 上分别构建；建议用 GitHub Actions 矩阵（`windows-latest` / `macos-latest` / `ubuntu-latest`）。
- **Python 版本**：打包器对 3.14 的支持已跟上（PyInstaller ≥6.15）。但 **OCRmyPDF 从未显式声明支持 3.14**（文档只写 “Python 3.11 or newer，3.12+ recommended”），且本项目用的是 `ocrmypdf.api._pdf_to_hocr` 这类**私有实验 API**。因此建议**发布基线用 Python 3.13**，在 3.14 上跑通端到端（上传→OCR→编辑→finalize）后再切换。

---

## 3. 桌面窗口怎么做

| 方案 | 原生窗口 | 增量体积 | 复杂度 | 评价 |
|---|---|---|---|---|
| 系统浏览器 `webbrowser.open` | ❌（只是标签页） | ~0 | 极低 | 快速验证可用，但不像「App」：无任务栏/托盘，误关标签即失去 UI |
| **pywebview** | ✅ 系统 WebView | 小 | 低 | **推荐** |
| Tauri v2 + sidecar | ✅ | 外壳小，但要 Rust 工具链 | 高 | 产品级外壳最强（内置托盘/单实例/更新），代价是双工具链 |
| Electron | ✅ | 大（Chromium，150–250MB） | 中高 | 除非已有 Node 团队，否则不划算 |
| Briefcase + Toga WebView | ✅ | 中 | 中 | 受限于外部二进制问题（§2.1） |

**推荐 pywebview** 的理由：

- 各平台复用系统 WebView（Windows WebView2 / macOS WKWebView / Linux WebKit2GTK），体积增量最小；
- 与本项目「前后端已分离、前端零构建」的形态天然契合——窗口只需指向 `http://127.0.0.1:<port>`；
- **注意**：FastAPI 是 ASGI 而非 WSGI，pywebview 的「直接传入应用对象」只支持 WSGI，所以正确做法是「后台线程跑 uvicorn + 窗口指向 localhost URL」；
- Linux 上 WebKit2GTK 是额外系统依赖；pywebview 会收集所有已安装的 GUI 后端，spec 里需要 `excludes`（[pywebview freezing 指南](https://pywebview.flowrl.com/guide/freezing.html)）。

**窗口与服务器的线程分工**（关键）：

- uvicorn **只在主线程安装信号处理器**（`uvicorn/server.py:325` 有 `if threading.current_thread() is not threading.main_thread(): return`）；
- `webview.start()` 会阻塞主线程跑 GUI 循环；
- 因此：**uvicorn 放后台线程**（用 `server.should_exit = True` 停止），**主线程留给 pywebview**。

---

## 4. 外部原生依赖：tesseract 与 ghostscript

OCRmyPDF 通过 subprocess 调用外部程序，且**按名字在 `PATH` 上查找**（已核验：`ocrmypdf/_exec/tesseract.py` 的 `ToolProbe(program='tesseract')`；`ghostscript.py` 的 `GS = 'gswin64c' if os.name == 'nt' else 'gs'`）。

几个对打包有直接影响的精确事实：

- 官方文档明确：**「OCRmyPDF will search the `PATH` environment variable to locate the binaries. By modifying the `PATH` environment variable, you can override the binaries that OCRmyPDF uses.」**（[Advanced features](https://ocrmypdf.readthedocs.io/en/stable/advanced.html)）。
- **不存在** `TESSERACT_PATH` / `GHOSTSCRIPT_PATH` 这类指向程序的环境变量——所以**捆绑的唯一正解就是把自带 `bin` 目录前置到 `PATH`**（或用绝对路径自己调用）。
- Windows 上查找顺序是 `PATH` → 注册表 → `Program Files` 扫描；若 `PATH` 未命中，会**回落到系统安装**，可能用错版本。Windows 上 `gs` 的实际可执行名是 **`gswin64c.exe`**（需按此名提供或建别名）。
- 语言数据用 `TESSDATA_PREFIX`，且它指向的是 **`tessdata/` 的父目录**（OCRmyPDF 本身不管理该变量，由 tesseract 读取）。

### 4.1 Ghostscript：默认已可完全省略（重要）

- 已核验本仓库 `.venv` 内 `ocrmypdf 17.11.0` 的 `Requires-Dist`：**包含 `pypdfium2>=5.0.0`，不包含 ghostscript**。
- OCRmyPDF 17 的 `--rasterizer auto`（默认）**优先用 `pypdfium2`** 光栅化；仅当 `--output-type pdfa*` 或显式 `--rasterizer ghostscript` 时才需要 `gs`。
- 本项目默认 `ocrmypdf_output_type = "pdf"`（`backend/config.py`），所以**默认路径不调用 gs**。
- 结论：**桌面版默认构建可以不带 Ghostscript**；若要支持用户在设置里选 PDF/A，要么随包附带 gs（引入 AGPL 议题，见 §5），要么在 UI 上禁用/提示该选项。

### 4.2 Tesseract：按引擎选择决定是否捆绑

- 本项目支持三个引擎：`unlimited`（API，**不需要本地二进制**）、`tesseract`（**需要** `tesseract` + 语言包）、`none`。
- 若希望「离线也能用」，就必须捆绑 tesseract；否则可以只支持 Unlimited API 引擎（但那样应用就失去了「无 key 也能跑」的能力）。
- 语言包体积：`chi_sim.traineddata` / `eng.traineddata` 通常在 **十几 MB 到几十 MB** 量级（`tessdata_best` 更大、`tessdata_fast` 更小）。
- 需要设置 `TESSDATA_PREFIX` 指向自带的 `tessdata/` 目录，或把 `tessdata/` 放在 tesseract 可执行文件同级的约定位置。

### 4.3 PyInstaller 特有陷阱：子进程环境污染

冻结后 PyInstaller 会修改库搜索路径（Linux `LD_LIBRARY_PATH`、Windows `SetDllDirectoryW`、macOS `DYLD_LIBRARY_PATH`），这些修改会**继承给被 spawn 的 tesseract**，可能导致它加载到不兼容的捆绑库而崩溃。官方要求在启动外部程序前净化环境（[PyInstaller common issues](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)）：

- POSIX：用 `LD_LIBRARY_PATH_ORIG` / 清理 `DYLD_LIBRARY_PATH`；
- Windows：`ctypes.windll.kernel32.SetDllDirectoryW(None)`。

另外 **macOS 从 Finder 启动时 `PATH` 只有 `/usr/bin:/bin:/usr/sbin:/sbin`**，Homebrew 路径不可见——必须用绝对路径调用自带二进制。

---

## 5. 许可证风险（**动手前必须先决策**）

### 5.1 依赖许可证汇总（均已从安装元数据核验）

| 组件 | 许可证 | 分发义务 / 风险 |
|---|---|---|
| OCRmyPDF | **MPL-2.0** | 宽松（文件级 copyleft）；修改其文件需回馈该文件 |
| pikepdf（含 libqpdf） | **MPL-2.0** | 宽松 |
| pypdfium2 / PDFium | **BSD-3-Clause / Apache-2.0** | 宽松 |
| Tesseract | Apache-2.0 | 宽松（保留声明） |
| fpdf2（OCRmyPDF 的 hOCR 渲染器） | LGPL-3.0-only | 作为独立库引入即可：保留许可声明、不静态吸收其代码 |
| Ghostscript（若捆绑） | AGPL-3.0（或 Artifex 商业授权） | ⚠️ 中风险；**默认已可不带**（见 §4.1） |

### 5.2 PyMuPDF 为什么曾是头号风险（历史记录）

> **状态：已解决。** 本节保留为决策依据。项目已按下面「路线 C」移除 PyMuPDF，`requirements.txt` 不再包含它，`.venv` 卸载后全套测试仍通过。

- PyMuPDF 是**进程内 `import` 的 Python 扩展**，与你的应用**链接成同一个程序**。AGPL-3.0 §5(c) 的立场是：对外分发时，整个组合作品需按 AGPL 授权并**提供完整对应源码**。
- 这对「想闭源分发」的桌面 App 是致命的；对「愿意整个应用以 AGPL 开源」的项目则没有问题。
- 关键限定：**AGPL 的义务由「分发」触发**。如果只是自己/组织内部使用、不向外分发二进制，则不触发。**「打包成 App」若含对外发布，就会触发。**
- 官方许可说明：[Artifex licensing](https://artifex.com/licensing/)、[GNU AGPL-3.0 全文](https://www.gnu.org/licenses/agpl-3.0.txt)。

### 5.3 三条可选路线

| 路线 | 做法 | 代价 |
|---|---|---|
| A. 整个应用按 AGPL 开源 | 遵从 AGPL 分发（附源码、许可、修改说明） | 免费，但放弃了闭源可能 |
| B. 购买 PyMuPDF 商业授权 | 向 Artifex 购买 | 有成本；但可闭源分发 |
| **C. 替换掉 PyMuPDF ✅ 已采用** | 用 `pypdfium2`（BSD/Apache，**本就是 OCRmyPDF 的强制依赖**）做页面渲染/预览/几何/文本抽取，`pikepdf` 做页面删除 | 已完成：`backend/pdf_processing.py` 成为唯一的 PDF 库访问层，移除 PyMuPDF |

**路线 C 的落地结果**（对应 `backend/pdf_processing.py`）：

- 全文只剩 **两个** PDF 库：`pypdfium2`（读/渲染/几何/文本）+ `pikepdf`（唯一的编辑操作：部分嵌入选页时删页）；
- 原 PyMuPDF 的 5 处使用点全部迁移：`pdf_processing`（预览渲染）、`validation`（文本层抽取）、`ocr_service`（页数/页面尺寸）、`page_store`（DPI 推导）、`image_export`（按 bbox 裁图）；
- 新增 `tests/test_pdf_processing.py` 用真实 PDF 覆盖渲染路径（含**左上原点的裁剪方向**与 **/Rotate 旋转页**两个易错点）。

> 路线 C 的额外好处：`pypdfium2` 是**纯 wheel**，比 PyMuPDF 更容易冻结打包。

---

## 6. 代码改造清单

### 6.1 路径：只读资源 vs 可写状态（**必须**）

冻结后 `__file__` 被重写到 `sys._MEIPASS`（PyInstaller 的解包目录）：onefile 下它是**每次启动新建、退出即删的临时目录**；onedir 下它是安装目录内的 `_internal`（普通用户**无写权限**，macOS `.app` 还叠加 Gatekeeper App Translocation）。因此当前所有 `Path(__file__).resolve().parent.parent` 写操作都会坏：

| 位置 | 现状 | 冻结后后果 |
|---|---|---|
| `backend/config.py:29-31` | `CONFIG_FILE = backend/ocr_config.toml` | WebUI **保存设置必失败**或保存后丢失 |
| `backend/ocr_service.py:41-42` | `UPLOAD_DIR` / `WORK_DIR` | 上传、hOCR、`job.json`、嵌入结果全部写坏 |
| `backend/cleanup.py:39-42` | `PROJECT_DIR` 等 | 清理逻辑扫错目录 |
| `backend/cli.py:144` | `output/` | 无头 CLI 输出目录错误 |
| `backend/main.py:91-92` | `FRONTEND_DIR` | 这是**只读资源**，应改从 `sys._MEIPASS` 解析 |

**正确模式**：新增 `backend/paths.py`，统一区分：

- 只读资源（`frontend/`、`config.example.toml`）→ `sys._MEIPASS` / Nuitka `sys.executable` 同级；
- 可写状态（`uploads/`、`work/`、`output/`）→ `platformdirs.user_data_dir(...)`；
- 可写配置（`ocr_config.toml`）→ `platformdirs.user_config_dir(...)`；
- 日志 → `platformdirs.user_log_dir(...)`。

开发态（未冻结）保持与现在完全一致的仓库相对路径，**保证 pytest 与 AGENTS.md 工作流不变**。示意实现：

```python
# backend/paths.py（新增）
from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "pdf-ocr-embed"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def resource_dir() -> Path:
    """只读打包资源根目录（frontend/ 等）。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)                       # PyInstaller
    if "__compiled__" in globals():
        return Path(sys.executable).resolve().parent    # Nuitka
    return Path(__file__).resolve().parent.parent       # 源码仓库根


def data_dir() -> Path:
    """可写状态根目录：uploads/ work/ output/。"""
    if is_frozen():
        from platformdirs import user_data_dir
        return Path(user_data_dir(APP_NAME, ensure_exists=True))
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    if is_frozen():
        from platformdirs import user_config_dir
        return Path(user_config_dir(APP_NAME, ensure_exists=True))
    return Path(__file__).resolve().parent            # 开发态：backend/


def log_dir() -> Path:
    if is_frozen():
        from platformdirs import user_log_dir
        return Path(user_log_dir(APP_NAME, ensure_exists=True))
    return data_dir() / "logs"


UPLOAD_DIR = data_dir() / "uploads"
WORK_DIR = data_dir() / "work"
OUTPUT_DIR = data_dir() / "output"
CONFIG_FILE = config_dir() / "ocr_config.toml"
FRONTEND_DIR = resource_dir() / "frontend"
```

需要同步的改动：

- `config.py`：`CONFIG_FILE` 取 `paths.CONFIG_FILE`；**写前 `mkdir(parents=True, exist_ok=True)`**；首次运行可从 `config.example.toml` 播种。
- `ocr_service.py` / `cleanup.py` / `cli.py` / `main.py`：改引用 `paths.*`。
- `requirements.txt`：新增 `platformdirs>=4.0`。
- ⚠️ **AGENTS.md 约束**：「不得在 `backend/config.py` 之外读取 `os.environ`」。若 `paths.py` 要保留环境变量逃生阀（如 `PDF_OCR_EMBED_DATA_DIR`），需先在 AGENTS.md 中把它登记为例外，或干脆不提供逃生阀。
- ⚠️ 现有 `tests/` 中约 **9 个测试文件**依赖这些模块级路径常量，改造时需一并调整（或通过 monkeypatch `paths` 常量保持兼容）。

### 6.2 服务器启动方式（**必须**）

现状 `backend/main.py:1149-1151`：

```python
def run() -> None:
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
```

三个问题都会在冻结后致命：

1. **`reload=True`**：uvicorn 的 reloader 用「字符串导入 + 用 `sys.executable` 起子进程重跑」实现。冻结后 `sys.executable` **就是你的 exe**，于是无限递归启动自己（狂占 CPU / 反复绑端口）。
2. **`"backend.main:app"` 字符串**：需要 `importlib.import_module`，依赖 hidden import 完整；直接传 **app 对象**最稳。
3. **`host="0.0.0.0"` + 固定 8000**：桌面应用应只绑 `127.0.0.1`，并用**内核分配的空闲端口**（bind 0）。

推荐实现（预绑定 socket，消除「探测端口→关闭→再绑定」的竞态）：

```python
# backend/server.py（新增）或直接放 desktop.py
import socket
import threading
import time

import uvicorn


def bind_loopback() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))          # 0 = 内核分配
    sock.listen(128)
    return sock, sock.getsockname()[1]


class EmbeddedServer:
    def __init__(self, app):
        self.sock, self.port = bind_loopback()
        self.url = f"http://127.0.0.1:{self.port}/"
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                             reload=False, workers=1, access_log=False)
        self.server = uvicorn.Server(cfg)
        # uvicorn 只在主线程装信号处理器 → 必须放后台线程
        self._thread = threading.Thread(target=self.server.run,
                                        kwargs={"sockets": [self.sock]},
                                        daemon=True, name="uvicorn")

    def start(self, timeout: float = 15.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("embedded server failed to start")
            time.sleep(0.05)

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True       # 线程内唯一的停止手段
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.server.force_exit = True
            self._thread.join(2.0)
```

### 6.3 冻结入口（**必须**）

新增 `desktop.py`（PyInstaller 的入口脚本）：

```python
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()       # 必须在任何重型 import 之前
    # 1) 起 EmbeddedServer
    # 2) 用 pywebview 打开 server.url（失败则 webbrowser.open 退化）
    # 3) 窗口关闭 → 先请求取消在跑的 OCR（写 <job_dir>/cancel）→ server.stop()
```

- **`multiprocessing.freeze_support()` 是硬性要求**：本项目自身用 threading，但 OCRmyPDF 在 `use_threads=False` 时会走 `ProcessPoolExecutor`，冻结后子进程会重跑主程序（[PyInstaller common issues](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)）。本项目所有 OCR 调用都固定 `use_threads=True`（AGENTS.md 硬约束），**打包后绝不能破坏这个约束**。
- **优雅退出**：复用项目已有的取消契约——写 `work/<job_id>/cancel`（`page_store.request_cancel`），让插件在页边界停止，避免硬杀丢页；退出前还应 `terminate()` 仍在跑的 tesseract 子进程，避免孤儿进程。

### 6.4 插件发现与 entry point（需实测）

- 冻结后 `importlib.metadata` 依赖 site-packages 的 `*.dist-info`，PyInstaller **默认不收集**，`plugin_auto_loaded()` 可能枚举不到插件（社区案例：[OCRmyPDF #659](https://github.com/ocrmypdf/OCRmyPDF/issues/659)、[#1033](https://github.com/ocrmypdf/OCRmyPDF/issues/1033)）。
- 本项目当前是**源码目录形式**（`ocrmypdf_unlimited/` 未 pip 安装，已核验 `.venv` 中无该 entry point）。这反而是好事：冻结时用 `hiddenimports=["ocrmypdf_unlimited"] + collect_submodules(...)` 显式收集，并让 `plugin_auto_loaded()` 在冻结态返回 `False`，使 `plugin_path()` 返回 dotted module 名、由显式 `importlib.import_module` 加载。
- ⚠️ **避免双重注册**：如果既让 entry point 命中、又显式传 `plugins=[...]`，pluggy 会报「同一模块注册两次」（AGENTS.md 已警告）。因此**不要** `copy_metadata("ocrmypdf-unlimited")`。
- `ocrmypdf` 自身的元数据可能需要 `copy_metadata("ocrmypdf")`（它可能读自身版本）——**需实测**。

### 6.5 其它（建议）

- **日志**：Windows `--noconsole` 下 `sys.stdout/stderr` 为 `None`，而 `logging_config.setup_logging()` 用 `StreamHandler()` 写 stderr，会抛 `AttributeError`。冻结态应改为 `FileHandler(paths.log_dir()/"app.log")` 并对 `None` 做保护。
- **CORS 收紧**：`main.py` 的 `allow_origins=["*"]` 在本地桌面场景应改为仅允许 `http://127.0.0.1:<port>`。
- **单实例**：用文件锁（`filelock` / `portalocker`，锁文件放 `platformdirs.user_runtime_dir()`）比端口探测更稳。
- **托盘/通知**：`pystray`（无内置托盘的 pywebview 需要它）。
- **签名/公证**：macOS 不签名 + 公证会触发 App Translocation（导致路径错乱）；Windows 需代码签名避免 SmartScreen 警告。CI 里统一处理。

### 6.6 打包 spec 要点

```python
# packaging/pdf_ocr_embed.spec（要点）
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

datas = [("frontend", "frontend")] + collect_data_files("ocrmypdf")
hiddenimports = (
    collect_submodules("backend")
    + collect_submodules("ocrmypdf_unlimited")
    + ["ocrmypdf_unlimited",
       "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
       "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"]
)
# 入口 desktop.py；onedir；不要把 tesseract 语言包漏掉（若捆绑）
# 不要 copy_metadata("ocrmypdf-unlimited")
```

---

## 7. 推荐方案与路线图

### 7.1 目标架构

```
┌─────────────────────────────────────────────┐
│  desktop.py（PyInstaller 入口，主线程）      │
│   ├─ multiprocessing.freeze_support()        │
│   ├─ pywebview 窗口 → http://127.0.0.1:<随机>│
│   └─ 退出：写 cancel → EmbeddedServer.stop() │
├─────────────────────────────────────────────┤
│  EmbeddedServer（后台线程，uvicorn.Server）  │
│   └─ backend.main:app（FastAPI，只绑 127.0.0.1）│
├─────────────────────────────────────────────┤
│  OCRmyPDF（进程内 API，use_threads=True）    │
│   ├─ pypdfium2 光栅化（无需 Ghostscript）    │
│   └─ subprocess → tesseract（随包或系统）    │
└─────────────────────────────────────────────┘
只读资源: sys._MEIPASS/frontend
可写状态: platformdirs.user_data_dir()/…  （uploads/ work/ output/ config/ logs/）
```

### 7.2 分阶段路线图

| 阶段 | 内容 | 产出 | 预估 |
|---|---|---|---|
| **P0 决策** | 定许可证路线（§5.3 A/B/C）；定是否捆绑 tesseract；定是否保留 PDF/A 输出 | 一页决策记录 | 0.5 天 |
| **P1 路径与启动重构** | 新增 `backend/paths.py`；改 5 处引用 + 9 个测试；重写 `run()`；新增 `desktop.py`（先用 `webbrowser.open`）；冻结态关闭 entry point 探测 | 代码可在源码态与冻结态同样工作 | 2–3 天 |
| **P2 首次打包冒烟** | PyInstaller onedir spec；收集 frontend/ocrmypdf/plugin；Windows 或 Linux 单平台先跑通「上传→OCR→编辑→finalize→下载」 | 可双击运行的 onedir | 2–4 天 |
| **P3 窗口与外壳** | 接 pywebview；优雅退出（cancel + 杀子进程）；单实例；日志落文件；CORS 收紧 | 真·桌面 App | 2–3 天 |
| **P4 外部二进制** | 捆绑 tesseract + tessdata，设 `TESSDATA_PREFIX`；子进程环境净化；若不捆绑则做缺依赖的友好提示 | 离线可用 | 1–3 天 |
| **P5 三平台 + 安装器** | CI 矩阵构建；Windows 安装器 / macOS `.dmg`+签名公证 / Linux AppImage | 可分发安装包 | 4–8 天 |
| ~~**P6（可选）**~~ ✅ 已完成 | ~~用 `pypdfium2` 替换 PyMuPDF~~ **已落地**（`backend/pdf_processing.py` + `tests/test_pdf_processing.py`） | 已消除 AGPL | — |
| **P6（可选，剩余）** | 托盘/通知；自动更新检查；项目许可证落定（现已无 AGPL 阻碍，可自由选 MIT/Apache-2.0） | 体验完善 | 3–5 天 |

### 7.3 最小可行验证（建议立刻做）

在投入 P1–P3 之前，用**半天**做一次「技术验证」，把最大不确定性打掉：

1. 写一个最小 `_probe.py`：`uvicorn.Server(Config(app, reload=False))` + 后台线程 + 随机端口 + `webbrowser.open`；
2. 用 PyInstaller onedir（不改业务代码，仅 monkeypatch 路径到临时目录）打一次包；
3. 验证三件事：**前端能加载**、**`import ocrmypdf` 与 `import ocrmypdf_unlimited` 成功**、**`ocrmypdf._pdf_to_hocr` 能调用外部 tesseract**。

这一步能提前暴露 80% 的冻结坑（hidden import、entry point、子进程环境、原生库冲突）。

---

## 8. 风险与未决问题

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | ~~**PyMuPDF AGPL**~~ ✅ 已解决 | ~~无法闭源分发~~ 已无 AGPL 依赖 | §5.3 路线 C 已落地；许可证可自由选择 |
| 2 | OCRmyPDF 未显式支持 Python 3.14 | 私有 API `_pdf_to_hocr` 在 3.14 上可能未验证 | 发布基线用 3.13；3.14 单独跑端到端 |
| 3 | 冻结后 entry point 枚举行为 | 插件加载失败 / 双重注册 | 冻结态强制 dotted module；**不** copy 插件元数据；实测 |
| 4 | 子进程环境污染 | tesseract 崩溃 | 启动外部程序前净化 `LD_LIBRARY_PATH` / `SetDllDirectoryW` |
| 5 | onefile + macOS `.app` | PyInstaller v7 将禁止 | 直接用 onedir + 安装器 |
| 6 | 长任务退出丢页 | 数据损坏 | 退出前写 `cancel` 并在页边界停止 |
| 7 | `paths.py` 环境变量逃生阀 | 违反 AGENTS.md 约束 | 同步更新 AGENTS.md 或取消该逃生阀 |
| 8 | macOS 未签名/未公证 | App Translocation → 路径错乱 | CI 中 codesign + notarytool |
| 9 | 体积/冷启动 | 用户体验 | onedir + 安装器；评估剔除不用的依赖 |

**未核实项**（建议自行验证）：

- OCRmyPDF 17.x 的 CI 是否覆盖 Python 3.14（仅有「支持最近三个 Python 版本」的间接表述）。
- `copy_metadata("ocrmypdf")` 是否必需——取决于 ocrmypdf 运行期是否读自身版本元数据。
- 各平台捆绑 tesseract 的具体体积（未实测构建产物）。
- PyInstaller 对**源码目录形式**插件（`ocrmypdf_unlimited/`）的收集行为需在真实 spec 下验证。

---

## 附录：参考链接

**打包工具**
- [PyInstaller Changelog](https://pyinstaller.org/en/stable/CHANGES.html)（Python 3.14 支持始于 6.15.0）
- [PyInstaller Run-time Information](https://pyinstaller.org/en/stable/runtime-information.html) · [Common Issues and Pitfalls](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html) · [Spec Files](https://pyinstaller.org/en/stable/spec-files.html) · [Hooks](https://pyinstaller.org/en/stable/hooks.html)
- [Nuitka 4.2 发布](https://nuitka.net/posts/nuitka-release-42.html) · [Nuitka User Manual](https://nuitka.net/doc/user-manual.html)
- [Briefcase Releases](https://briefcase.beeware.org/en/latest/about/releases/) · [cx_Freeze PyPI](https://pypi.org/project/cx-Freeze/)

**桌面外壳**
- [pywebview 文档](https://pywebview.flowrl.com/) · [Freezing 指南](https://pywebview.flowrl.com/guide/freezing.html)
- [Tauri v2 Sidecar](https://v2.tauri.app/develop/sidecar/) · [Tauri Single Instance](https://v2.tauri.app/plugin/single-instance/)
- [pystray](https://pystray.readthedocs.io/) · [platformdirs](https://platformdirs.readthedocs.io/en/latest/api.html) · [filelock](https://pypi.org/project/filelock/) · [portalocker](https://portalocker.readthedocs.io/)

**OCR 与原生依赖**
- [OCRmyPDF 安装文档](https://ocrmypdf.readthedocs.io/en/latest/installation.html) · [插件文档](https://ocrmypdf.readthedocs.io/en/latest/plugins.html) · [17.x release notes](https://ocrmypdf.readthedocs.io/en/latest/releasenotes/version17.html)
- [uvicorn Deployment](https://www.uvicorn.org/deployment/)

**许可证**
- [GNU AGPL-3.0 全文](https://www.gnu.org/licenses/agpl-3.0.txt) · [Artifex 许可说明](https://artifex.com/licensing/)

**相关 issue**
- [OCRmyPDF #659](https://github.com/ocrmypdf/OCRmyPDF/issues/659) · [#1033](https://github.com/ocrmypdf/OCRmyPDF/issues/1033)（冻结 + 元数据）
- [PyMuPDF #712](https://github.com/pymupdf/PyMuPDF/issues/712)（冻结导入）
- [uvicorn #1820](https://github.com/Kludex/uvicorn/discussions/1820)（PyInstaller 下 reload/workers 问题）
