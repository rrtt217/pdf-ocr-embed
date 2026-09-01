# PDF OCR Embed — 跨平台 GUI/WebUI：纯图片 PDF 嵌入文字（OCRmyPDF 架构）

## 目标
做一个跨平台的桌面 GUI / WebUI 程序：输入纯图片 PDF（扫描件），通过 OCR 识别的文字
按坐标**嵌入**到 PDF 内层（真正的可选中/可搜索文字层），保留原图作为背景。

> **架构决定（rebuild/ocrmypdf-backend 分支）**：后端推倒自研管线，改用
> **OCRmyPDF**（https://github.com/ocrmypdf/OCRmyPDF）作为 OCR 核心——栅格化、
> 引擎调度、并发、文本层渲染（fpdf2）、graft 回写、PDF/A 与优化全部交给它；
> **unlimited-ocr 以 OCRmyPDF 插件（`OcrEngine`）实现**（`backend/ocrmypad`）。
> 前端**沿用现有自研 WebUI**（不采用 OCRmyPDF 的 `misc/_webservice.py`，理由见下）。

## 为什么沿用自研前端（而非 misc/_webservice.py）

`misc/_webservice.py` 是一个 **Streamlit** 应用（并非简单 HTTP 服务）：表单式参数
页 → 调 ocrmypdf CLI → 展示结果。它不满足本项目核心交互：

1. **无逐页文本编辑**：本项目的核心是浏览器内逐块编辑识别文本（错字修正、块
   移动/缩放/合并），`_webservice.py` 没有任何编辑界面。
2. **无进度事件流**：它同步等待整份 PDF 处理完成；本项目需要 SSE 每页进度 +
   任务卡片 + 并行多任务。
3. **无编辑回写通道**：本项目用 `_pdf_to_hocr` + `_hocr_to_ocr_pdf` 官方 API 实现
   「OCR → 编辑 → 合成」闭环，`_webservice.py` 只走单次 `ocr()`。
4. **技术栈冲突**：引入 Streamlit 意味着第二个端口、第二套 UI 框架和状态模型；
   现有前端是零构建原生 JS，由 FastAPI 直接托管。

结论：**保留现有前端**（`frontend/` 原生 JS，`/api/*` 契约基本不变），仅把后端
管线整体替换为 OCRmyPDF；`_webservice.py` 仅作为「无需编辑、快速 OCR」的参考
实现保留在上游。

## 技术栈
- **OCR 核心**：OCRmyPDF ≥17.11（`pip install ocrmypdf`；系统依赖 tesseract、
  ghostscript）。文本层渲染用其内置 **fpdf2** 渲染器（无需 qpdf）。
- **后端**：FastAPI + `ocrmypdf.api`（进程内调用，不 shell out）。
- **unlimited-ocr 引擎**：OpenAI 兼容视觉 API（USTC `unlimited-ocr` 模型），输出
  `<|det|>` 标记流，由插件解析。
- **前端**：沿用单页 WebUI（原生 JS，零构建）。
- 不引入 CUDA / NVIDIA 依赖。

## OCRmyPDF 插件（backend/ocrmypad/）

OCRmyPDF 的 `OcrEngine` 插件接口（`ocrmypdf.pluginspec`）：

```python
class OcrEngine(ABC):
    @staticmethod
    def generate_hocr(input_file, output_hocr, output_text, options): ...
    # 新式 API（返回 OcrElement 树）：generate_ocr()（本项目用 hOCR 路径）
    @staticmethod
    def version() / creator_tag(options) / languages(options)
    @staticmethod
    def get_orientation(...) / get_deskew(...)
```

`backend/ocrmypad/` 包结构：

| 模块 | 职责 |
| --- | --- |
| `unlimited_engine.py` | `UnlimitedOcrEngine(OcrEngine)`：逐页调 API → 解析 → 写 hOCR + 块 sidecar + 文本 sidecar；`get_ocr_engine` hook（`ocr_engine='unlimited'` 时接管） |
| `engine_client.py` | OpenAI 兼容客户端（`skip_special_tokens=False`、截断检测、退化重试、超时随 max_tokens 缩放） |
| `parser.py` | `<|det|>` 标记 → `Block`（1000×1000 画布 → 原始像素坐标）→ hOCR 文档（`div.ocr_page`/`p.ocr_par`/`span.ocr_line`/`span.ocrx_word`，含 `scan_res`） |
| `text_norm.py` | 数学/表格/LaTeX 文本规范化（自研管线平移，逻辑不变） |
| `progress.py` | 每页进度注册表 + 取消标志（引擎在 ocrmypdf 工作线程内汇报，SSE 读取） |

关键事实（ocrmypdf 17.11 实测）：
- `ocrmypdf.api._pdf_to_hocr(input_pdf, output_folder, plugins=[...], ...)`：跑到
  每页 hOCR，工作文件夹布局 `{output_folder}/origin.pdf`、
  `000001_ocr_hocr.hocr`、`000001_hocr.json`。本项目约定
  `output_folder = work/<job_id>/hocr`，引擎据此汇报进度。
- `ocrmypdf.api._hocr_to_ocr_pdf(work_folder, output_file, ...)`：把（可编辑后的）
  hOCR 用 fpdf2 渲染成隐形文字层、graft 回原页、跑后处理（PDF/A、优化）。
  这是官方提供的**编辑回写**通道。
- 引擎 `languages()` 返回请求语言 ∪ `{"und"}`（引擎语言无关）。
- 插件基础设施用读写锁：同插件集的并发任务可重叠；`use_threads=True` 必需
  （HTTP IO-bound，且线程化运行让 progress 注册表留在本进程）。

## 数据流（任务流水线）

1. `POST /api/ocr/upload` → `create_job`（fitz 校验 + 页数）→ 后台线程跑 OCR 阶段。
2. **OCR 阶段**：`_pdf_to_hocr`（`mode=force` 等 OcrOptions 来自 config + 请求覆盖）
   → 插件引擎逐页 OCR → `work/<job>/hocr/000001_ocr_hocr.hocr` +
   `000001_ocr_hocr.blocks.json`（WebUI 的可编辑表示）+ sidecar 文本。
3. **编辑**：`POST /api/pages/{job}/{i}` → 编辑写回块 sidecar，并从块**重新生成**
   该页 hOCR（finalize 渲染编辑后的文本）。
4. **合成**：`POST /api/embed/{job}` → `_hocr_to_ocr_pdf` → `work/<job>/embedded.pdf`
   → 嵌后校验报告（`backend/validation.py`，逻辑不变）。
5. `GET /api/download/{job_id}.pdf` 下载；`GET /api/ocr/zip?jobs=...` 打包。

## 硬不变量（不破坏）

- 块 bbox 是 `[x1, y1, x2, y2]` **整数、原始像素空间**（top-left origin）。
  1000×1000 归一化画布 → 原始像素的换算集中在 `backend/errors.normalize_bbox`
  （parser 与 sidecar 数据都经它），hOCR 的 `scan_res` 必须携带真实 DPI。
- 引擎原始输出（标记流）不出 `backend/ocrmypad`；其余代码只看块 sidecar JSON。
- API key/provider 仅来自外部配置（TOML `backend/ocr_config.toml` + WebUI 保存 +
  `OCR_*` 环境变量最高优先级覆盖，全部经 `backend/config.resolve()`）。
- `max_tokens` 必须 < 32768。
- 运行时产物（`output/`、`work/`、`uploads/`、`.venv/`、`backend/ocr_config.toml`）
  已 gitignore；绝不提交 key 或大样本 PDF。

## 配置键位（backend/ocr_config.toml）

- `provider` / `api_key` / `base_url` / `model`：unlimited 引擎的 API 凭据。
- `ocr_engine`：`unlimited`（插件，默认）| `tesseract`（ocrmypdf 内置）| `none`。
- `ocrmypdf_mode`（force|skip|redo|default）、`ocrmypdf_jobs`（0=auto）、
  `ocrmypdf_optimize`（0..3）、`ocrmypdf_output_type`（pdf|pdfa）、
  `ocrmypdf_language` / `ocrmypdf_deskew` / `ocrmypdf_clean` / `ocrmypdf_rotate_pages`。
- HTTP 重试限速：`max_retries` / `retry_base_delay` / `retry_max_delay` / `rate_limit_rps`。
- 清理与日志：`cleanup_max_age_hours` / `cleanup_interval_hours` / `log_level`。
- 环境变量映射：`OCR_API_KEY`（`USTC_API_KEY` 别名）、`OCR_BASE_URL`、`OCR_MODEL`、
  `OCR_ENGINE`、`OCRMYPDF_*`、`OCR_MAX_RETRIES` 等（见 `backend/config.py::_ENV_ALIASES`）。

## API 一览（与旧版兼容面）

| 端点 | 说明 |
| --- | --- |
| `GET /api/health` | 状态 + 引擎映射（unlimited/tesseract/none） |
| `GET/POST /api/settings` | provider 配置（key 打码）+ 流水线旋钮 |
| `POST /api/ocr/upload` | `files`（多文件）或 `file`；`ocr_engine`、`concurrency`（→ ocrmypdf jobs） |
| `GET /api/ocr/stream/{job_id}` | SSE：status + 每页 `progress` 事件 |
| `POST /api/ocr/retry/{job_id}` | `page_start/page_end/force` 页范围重跑 |
| `POST /api/ocr/stop/{job_id}` | 请求停止（引擎逐页检查取消标志） |
| `GET /api/pages/{job_id}` | 已完成页（块 sidecar JSON，含 `parse_version`） |
| `POST /api/pages/{job_id}/{i}` | 编辑页 → 写 sidecar + 重生成 hOCR |
| `GET /api/pages/{job_id}/{i}/image` | 页面预览 PNG（PyMuPDF 渲染） |
| `POST /api/embed/{job_id}` | finalize（`optimize`、`output_type`）+ 校验报告 |
| `GET /api/validation/{job_id}` | 按需重跑校验 |
| `GET /api/download/{job_id}.pdf` | 下载结果 |
| `GET /api/ocr/zip?jobs=...` | 批量打包下载 |

已移除（随自研管线退役）：`/api/cache*`（结果缓存）、`/api/fonts`、
`/api/preview/*`（调试叠加）、`/api/fontinfo/*`、预处理开关。

## 执行入口与可靠性

- Web：`backend/main.py`；无头 CLI：`python -m backend.cli input.pdf -o out.pdf
  --pages 1-3 --engine unlimited --sidecar-text`。
- 引擎 API 调用走 `backend/http_retry.py`：429/5xx/瞬时网络错误指数退避重试
  （尊重 `Retry-After`）+ 线程安全限速；日志绝不回显 key（`config.redact_secrets`）。
- 任务持久化：`work/<job>/job.json`，重启恢复（中断的任务标 `stopped`，hOCR 工作
  文件夹保留——已识别页可直接合成，无需重传）。
- 后台清理：`backend/cleanup.py` 周期删除未引用的 `work/`、`output/`、`uploads/`
  旧文件（live job 引用的路径豁免）。

## 测试

`.venv/bin/python -m pytest`（140 项）：标记解析/hOCR 生成、坐标换算、插件注册、
页面选择、任务持久化、配置优先级、API 守卫、密钥脱敏、批量上传/ZIP、校验、CLI。
端到端：`python -m backend.cli`（真实 API 冒烟已验证：OCR → 编辑 → 合成 →
提取文本与编辑一致，覆盖率 1.0）。

## 交付
项目维护于本仓库 `pdf-ocr-embed`（分支 `rebuild/ocrmypdf-backend`）；文档与测试
随功能同步更新。
