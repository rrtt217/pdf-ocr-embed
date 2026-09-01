# PDF OCR Embed

> 中文 ｜ [English](README.en.md)

跨平台的纯图片 PDF → 内嵌可搜索/可选中文字层工具。上传扫描 PDF，OCR 识别文字，
按识别坐标把文字**不可见地嵌入** PDF 内层（搜索、复制、选择可用，视觉不叠加），
同时提供 WebUI 编辑识别结果（修正错字）与文本框高亮预览。

本项目是**完全独立的程序**：API key 通过外部配置提供，代码里不硬编码任何密钥。

> **AI / Vibe coding 提示**：本项目的代码、设计与其他文档由 **Vibe coding** 生成
> （主要使用 DeepSeek V4 Flash 编写与迭代）。使用者请自行验证；二次开发时多审视
> 安全性、边界条件与依赖版本，别盲目信任 AI 输出。
>
> **给 AI Agent 的快速入口**：`AGENTS.md` 是为 AI 编码代理编写的项目指南（架构、
> 硬性约定，以及**如何编写新的 OCR adapter** 的完整步骤与检查清单）。改动前请先读它。

---

- **OCR 核心 = OCRmyPDF**：栅格化、引擎调度、并发、文本层渲染（内置 fpdf2 渲染器）、
  graft 回写、PDF/A 与优化全部交给 [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF)
  （≥17.11，系统依赖 tesseract + ghostscript，无需 qpdf）。后端通过
  `ocrmypdf.api` 进程内调用其官方**编辑回写**通道：
  `_pdf_to_hocr`（OCR → 每页 hOCR）+ `_hocr_to_ocr_pdf`（编辑后合成最终 PDF）。
- **unlimited-ocr 以 OCRmyPDF 插件实现**（`backend/ocrmypad/`）：一个
  `OcrEngine` 插件，逐页调 OpenAI 兼容视觉 API（USTC `unlimited-ocr` 模型），
  解析 `<|det|>type [bbox]<|/det|>content` 标记（1000×1000 归一化画布逐维缩放回
  **原始像素坐标**），写成 hOCR + 块 sidecar JSON（WebUI 的可编辑表示）。
  `ocr_engine = "tesseract"` 时回退 ocrmypdf 内置 Tesseract；`"none"` 关闭 OCR。
- **OCR 设置完全外部化**：本地 TOML 配置 `backend/ocr_config.toml` + WebUI 设置页
  （WebUI 保存时写入同一个 TOML 文件）。`OCR_*` **环境变量可选地覆盖**全部设置
  （优先级最高：环境变量 > WebUI 会话内保存值 > TOML 文件）；JSON / .env 文件
  配置已移除。
- **前端沿用自研 WebUI**（零构建原生 JS）：左侧可编辑文本块，右侧页面预览 +
  bbox 高亮框、设置表单、嵌入按钮、SSE 进度条。不采用 OCRmyPDF 的
  `misc/_webservice.py`（Streamlit 表单应用，无逐页编辑/进度流，理由见 DESIGN.md）。
- **进度流**：SSE 推送每页 OCR 进度（插件引擎在工作线程内汇报到共享注册表）。
- **并行 OCR**：`concurrency` 映射为 OCRmyPDF 的 worker 数（`ocrmypdf_jobs`）；
  `use_threads` 固定开启（HTTP IO-bound，且保持进度注册表在本进程）。
- **置信度审阅视图**：每个文本块带置信度徽标（85/60 分档绿/黄/红），低置信块红描边；
  「只看低置信度」过滤 + 可调阈值（默认 60%），页 tab 角标显示该页低置信块数。
- **输出选项**：合成时可选**优化级别**（0–3，交给 ocrmypdf 的 optimize 阶段）与
  **输出类型**（PDF / PDF/A）。
- **任务持久化**：任务状态实时写入 `work/<job_id>/job.json`；每页 OCR 结果以块
  sidecar 存于 `work/<job_id>/hocr/`，服务重启自动恢复——已识别页可直接合成，
  无需重传。
- **批量上传 + 打包下载**：一次拖入/选择多个 PDF，每个文件自动成为独立 OCR 任务
  （并行运行，各自有卡片与 SSE 进度）；在任务列表勾选已嵌入完成的任务，一键把它们的
  嵌入式 PDF 打包成一个 ZIP 下载（服务端逐文件流式打包，不整包读入内存）。
- **国际化（i18n）**：内置**英文 / 中文**两套界面，页头可随时切换（`frontend/i18n.js`），
  默认跟随浏览器语言；切换语言不刷新页面即时生效。
- **浅色 / 深色 / 自适应主题**：页头切换，选择记忆在 localStorage；自适应跟随系统
  `prefers-color-scheme`，深色下原生控件与滚动条同步变暗。
- **WebUI 体验优化**：Toast 通知、`Ctrl/⌘+Enter` 快速嵌入、`←/→` 翻页快捷键、
  主题/引擎等偏好本地记忆、焦点可见样式与 `prefers-reduced-motion` 支持，
  内嵌 SVG favicon 与随主题变化的 `theme-color`。
- **无 CUDA / NVIDIA** 依赖。

---

## 安装

**系统依赖**（OCRmyPDF 需要）：tesseract-ocr 与 ghostscript；无 qpdf 要求。

```bash
# Debian/Ubuntu
sudo apt-get install tesseract-ocr ghostscript
cd /home/david/vibe-arena/pdf-ocr-embed
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # 含 ocrmypdf>=17.11
```

## 配置 OCR

所有 OCR 设置都集中在一个 **TOML 配置文件** `backend/ocr_config.toml` 中
（文件已加入 `.gitignore`）。有两种填法：

### 1) 本地 TOML 配置文件（推荐）

从仓库根目录的示例文件复制一份再修改：

```bash
cp config.example.toml backend/ocr_config.toml
# 然后编辑 backend/ocr_config.toml 填入你的值
```

最小配置：

```toml
provider = "ustc"
api_key = "你的 key"
base_url = "https://api.llm.ustc.edu.cn/v1"
model = "unlimited-ocr"
```

任意 OpenAI 兼容端点均可，只需改 `base_url` + `model` 即可切换引擎。
完整的可配置项（Tesseract、通用 OpenAI 提示词、嵌入字体、临时文件清理、
日志级别等）见 `config.example.toml` 中的注释。

### 2) WebUI 设置页

启动后在网页右上角 **Settings** 填写保存（key 会被打码存储）。设置页打开时会
读取并预填 TOML 文件中的值，保存只回写 provider 相关的四项字段，不会改动文件
中的其它配置（tesseract / cleanup / log_level 等）；把某字段清空再保存则会将其
重置（随后回退到内置预设）。

> 未配置任何 key 时调用 OCR 会返回明确错误提示，其它功能（上传、预览）不受影响。

### 3) 环境变量覆盖（可选）

任何一条 `OCR_*` 环境变量都会**覆盖** TOML 文件与 WebUI 会话内的同名字段
（优先级：环境变量 > WebUI 保存值 > TOML 文件），适合临时切换 key/端点/引擎
而无需动配置文件，例如：

```bash
OCR_API_KEY=sk-xxx OCR_BASE_URL=https://example.com/v1 python -m backend.main
```

`USTC_API_KEY` 是 `OCR_API_KEY` 的别名（仅当前者未设置时生效）。
环境变量与 TOML 键的对应关系如下：

| 环境变量 | TOML 键 |
| ---- | ---- |
| `OCR_API_KEY` / `USTC_API_KEY` | `api_key` |
| `OCR_BASE_URL` | `base_url` |
| `OCR_MODEL` | `model` |
| `OCR_PROVIDER` | `provider` |
| `OCR_ENGINE` | `ocr_engine` |
| `OCRMYPDF_MODE` | `ocrmypdf_mode` |
| `OCRMYPDF_JOBS` | `ocrmypdf_jobs` |
| `OCRMYPDF_OPTIMIZE` | `ocrmypdf_optimize` |
| `OCRMYPDF_OUTPUT_TYPE` | `ocrmypdf_output_type` |
| `OCRMYPDF_LANGUAGE` | `ocrmypdf_language` |
| `OCRMYPDF_DESKEW` | `ocrmypdf_deskew` |
| `OCRMYPDF_CLEAN` | `ocrmypdf_clean` |
| `OCRMYPDF_ROTATE_PAGES` | `ocrmypdf_rotate_pages` |
| `OCR_MAX_RETRIES` | `max_retries` |
| `OCR_RETRY_BASE_DELAY` | `retry_base_delay` |
| `OCR_RETRY_MAX_DELAY` | `retry_max_delay` |
| `OCR_RATE_LIMIT_RPS` | `rate_limit_rps` |
| `OCR_CLEANUP_MAX_AGE_HOURS` | `cleanup_max_age_hours` |
| `OCR_CLEANUP_INTERVAL_HOURS` | `cleanup_interval_hours` |
| `OCR_LOG_LEVEL` | `log_level` |

---

## 启动

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
# 或
python -m backend.main
```

浏览器打开 <http://localhost:8000>，拖入 PDF 即可。

### 选择 OCR 引擎

WebUI 上传区可下拉选择三个引擎（`ocr_engine`）：

- **Unlimited OCR (API)**（默认）— OCRmyPDF 插件引擎，需配置 API key / base_url /
  model（见上文）。
- **Tesseract (local)** — ocrmypdf 内置 Tesseract，**无需 API key**。在"OCR language"
  填语言包，如 `chi_sim`（中文）、`eng`（英文）、`chi_sim+eng`（中英混合）。
- **No OCR** — 不做 OCR（仅 ocrmypdf 的图像处理/优化）。

Settings 弹窗还提供流水线旋钮（同样写入 TOML）：`mode`（force-ocr | skip-text |
redo-ocr）、`language`（Tesseract）、`deskew`（纠偏）、`clean`（需 unpaper）。

命令行方式（tesseract 示例）：

```bash
# 把 ocrmypdf_language 写进 backend/ocr_config.toml（或 Settings 弹窗）
echo 'ocrmypdf_language = "chi_sim"' >> backend/ocr_config.toml
uvicorn backend.main:app --port 8000
```

### 批量上传与打包下载

上传区支持一次拖入或选择**多个 PDF**（单个上传依旧可用）。每个文件都会成为
**独立任务**：各自拥有卡片、SSE 进度流与持久化状态，可并行运行——没有单独的
队列管理器。所有上传并发发出后，列表直接从服务端 `/api/jobs` 回拉（服务端始终
是任务列表的唯一数据源），运行中的任务各自接上 SSE 进度。

任务完成并点击 **Embed invisible text** 嵌入后，其卡片上会出现**勾选框**：勾选
任意多个已嵌入完成的任务，再点任务列表右上角的 **⬇ Download ZIP**，即可把它们的
嵌入式 PDF（按源文件名命名，重名自动加序号）打包成一个 ZIP 下载。打包由服务端从
磁盘逐文件流式写入（对应 `GET /api/ocr/zip?jobs=id1,id2,...`），不会把整个 ZIP
读入内存；请求的任务中没有任何嵌入结果时返回 404，有结果的任务会正常包含在内。
### 嵌后自动校验 + 质量报告

嵌入完成后，后端会用 PyMuPDF 重新抽取嵌入输出的文字层，与 OCR 源文本逐页比对
（`backend/validation.py`），给出一份「这份 PDF 能不能信」的报告：

- 页级 **覆盖率**（容忍换行/标点差异的 token 重叠 + 字符连续度）、源/嵌字符数
  与词数、置信度统计（min / avg / max，按 <60% / 60–80% / ≥80% 分桶）。
- 汇总行：平均覆盖率、低于阈值（默认 60%）的页列表、无源文本页、总块数。
- 中文/日文等无空格文字按单字分词，覆盖率对 CJK 同样有意义。

使用：嵌入后响应自带 `report`；也可点工作区的 **Validate** 按钮随时
`GET /api/validation/{job_id}` 重跑校验。校验只读 artifact，不修改嵌入文件，
也不会让嵌入失败（校验出错时以 `ok:false` 呈现，不影响下载）。
### 块操作与编辑（#6）

左侧块编辑器与右侧预览区支持直接修整识别结果：

- **合并**：点击块顶部信息行选中多个块（或按住 Shift/Ctrl 在预览图上点选），
  再点工具栏 **Merge**——bbox 取并集、文本按换行拼接。
- **拆分**：把光标放到块文本中间，点块的 **Split**——沿长边按文本长度比例
  拆成两块（bbox 同步一分为二）。
- **新增**：点 **Add block** 后在预览图上拖出一个矩形（Esc 取消），生成空
  文本块，可在左侧输入文本，再拖动/缩放到合适位置。
- **移动 / 缩放**：预览图上直接拖动块移动；选中块的右下角有缩放手柄；
  聚焦某个块编辑框后可用**方向键**微调 bbox（Shift=10px）。
- **撤销**：每次结构性编辑（增/删/合并/拆分/移动/缩放）都记录快照，
  工具栏 **Undo** 逐级回退（仅当会话，不持久化）。
- 所有 bbox 调整都保持**整数像素坐标**并夹取在页面范围内（`x1<=x2`、`y1<=y2`），
  与既有坐标不变式一致；编辑结果沿用现有「嵌入时把整页数据发给服务端」的路径。

### 无头 CLI 模式（headless）

不启动 Web 服务，直接用命令行完成「OCR → 合成」整条流水线（复用
`backend.ocr_service` 同一套后端逻辑）：

```bash
python -m backend.cli book.pdf --engine tesseract --pages 1-20 --jobs 2
python -m backend.cli book.pdf --engine unlimited --pages 1-5 --sidecar-text  # 打印每页文本
```

参数：`--engine`（默认 unlimited）、`--pages`（1 起；`"1-20"` / `"1,3,5-7"` / `"1-"` / `"-5"`）、
`--jobs`（worker 数）、`--out`（默认 `output/`）、`--sidecar-text`。
输出 `<名>_embedded_<id>.pdf`。API key 等配置仍走 `resolve()`（TOML / 环境变量），
不做任何硬编码。

### API 速览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/` | WebUI 页面 |
| GET  | `/api/health` | 健康检查 + 引擎映射 |
| GET/POST | `/api/settings` | 读取 / 保存 provider 配置（打码）+ 流水线旋钮 |
| POST | `/api/ocr/upload` | 上传 PDF → OCRmyPDF 后台 OCR（`files` 多文件 / `file` 单文件；`ocr_engine` 引擎选择，`concurrency` → worker 数，`lang` 供 tesseract，`base_url/api_key/model` 覆盖）→ 返回 job id |
| GET  | `/api/ocr/zip?jobs=id1,id2` | 把所选任务的嵌入式 PDF 打包成一个 ZIP 下载（`jobs` 为逗号分隔的任务 id；请求的任务全都没有嵌入结果时返回 404） |
| POST | `/api/ocr/retry/{job_id}` | 对已上传但失败/中断的任务重跑 OCR（默认只跑缺失页，不重头开始；参数同 upload，另支持 `page_start`/`page_end` 页码范围、`force` 强制重跑已成功页） |
| POST | `/api/ocr/stop/{job_id}` | 中途停止正在运行的 OCR（已完成页保留，可下载或重试剩余） |
| GET  | `/api/logs` | 获取最近后端调试日志 |
| GET  | `/api/ocr/stream/{job_id}` | SSE 进度流（status + 每页 progress 事件） |
| GET  | `/api/pages/{job_id}` | 取全部分页 OCR 数据（块 sidecar JSON） |
| GET  | `/api/pages/{job_id}/{i}/image` | 页面预览 PNG |
| POST | `/api/pages/{job_id}/{i}` | 更新单个可编辑页（写 sidecar + 重生成 hOCR） |
| POST | `/api/embed/{job_id}` | 合成（可编辑后的）文字 → `embedded.pdf`（`optimize`、`output_type`） |
| GET  | `/api/download/{job_id}.pdf` | 下载嵌入结果 |
| GET  | `/api/cleanup` | 临时文件清理概况（未被任务引用的 work/output/uploads 文件数量与大小） |
| POST | `/api/cleanup/run` | 执行/预览清理（`older_than_hours` 保留时长、`dry_run` 预览、`force` 忽略时限，仍永不删任务在用文件） |

---

## 目录结构

```
pdf-ocr-embed/
├── backend/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 应用与全部路由
│   ├── config.py               # 外部设置解析（TOML 配置文件 / WebUI / OCR_* 环境变量）
│   ├── models.py               # 编辑器页 JSON 结构（OcrPage/OcrBlock 兼容）
│   ├── pdf_processing.py       # 页面预览渲染（PyMuPDF）
│   ├── ocr_service.py          # OCRmyPDF 编排（_pdf_to_hocr + _hocr_to_ocr_pdf）+ 任务/进度状态
│   ├── ocrmypad/               # OCRmyPDF 插件包（unlimited-ocr 引擎）
│   │   ├── unlimited_engine.py # OcrEngine 插件 + get_ocr_engine hook
│   │   ├── engine_client.py    # OpenAI 兼容客户端（截断检测/重试/超时）
│   │   ├── parser.py           # <|det|> 标记解析 + hOCR 生成
│   │   ├── text_norm.py        # 数学/表格文本规范化
│   │   └── progress.py         # 每页进度注册表 + 取消标志
│   ├── errors.py               # UnavailableError + 1000 画布 → 像素坐标换算
│   ├── http_retry.py           # HTTP 重试/限速（引擎 API 调用）
│   ├── validation.py           # 嵌后校验（覆盖率报告）
│   ├── batch.py                # ZIP 打包（流式）
│   ├── cleanup.py              # 临时文件清理
│   ├── logging_config.py       # 日志
│   └── cli.py                  # 无头 CLI（python -m backend.cli）
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── i18n.js                 # 英文 / 中文双语界面
├── tests/                      # pytest 测试（140 项）
├── requirements-dev.txt        # 开发依赖（pytest）
├── requirements.txt
├── config.example.toml
├── .gitignore
├── AGENTS.md     # 面向 AI 编码代理的项目指南（含插件扩展点）
└── DESIGN.md     # 设计文档（OCRmyPDF 架构 + 前端沿用理由）
```

---

## 说明与限制

- bbox 为 `[x1,y1,x2,y2]` **整数、原始像素空间**（top-left origin）。1000×1000
  归一化画布 → 真实像素的换算集中在 `backend/errors.normalize_bbox`，hOCR 的
  `scan_res` 携带真实 DPI（fpdf2 渲染器的 px→pt 变换依赖它）。
- `max_tokens` 默认 16384（必须 < 32768，否则 API 400）。响应若被截断
  （`finish_reason=length` 或 `completion_tokens >= max_tokens`），该页按**失败**
  处理并给出明确错误，重试即可补跑，不会把残缺结果当作成功。
- 坐标进入 PDF 层由 OCRmyPDF 完成：fpdf2 渲染器与 hOCR 同为 top-left 原点，
  无需 y 轴翻转；页面旋转由 ocrmypdf 的 rotate/graft 流程处理。
- **Tesseract 引擎（本地，无 key）**：ocrmypdf 内置实现。语言通过
  `ocrmypdf_language` 配置（中文 `chi_sim`，可组合 `chi_sim+eng`）；需系统装有
  `tesseract` 二进制 + 对应语言包。
- **unlimited 引擎结果处理**：`table` 块的 HTML 会转换成行列文本（不把 `<tr>/<td>`
  标签写进文本层）；公式/表格单元格会收紧模型的分词空格（`X _ p`→`X_p`、
  `f (x)`→`f(x)`）；相邻单个数字之间不会自动合并；`image_caption` 图注连同其 bbox
  （`caption_bbox`）保留并嵌入到文字层。
- **worker 数（concurrency / ocrmypdf_jobs）**：上传时可指定（1–32），映射为
  OCRmyPDF 的 OCR 并发 worker 数；`use_threads` 固定开启。并发越高对 API 的
  并发压力越大，需与引擎配额匹配。
- **失败重试（智能）**：OCR 报错或中途停止后，WebUI 显示 **Retry remaining** 按钮。
  重试**只重跑失败/未完成的页**（`force=true` 时重跑全部选中页）。也可调用
  `POST /api/ocr/retry/{job_id}`，复用已上传的 PDF，无需重新上传。
- **中途停止**：OCR 运行中可点击 **Stop** 按钮或调用 `POST /api/ocr/stop/{job_id}`
  停止（引擎逐页检查取消标志）。已完成的页保留（sidecar 在磁盘上），可
  **Retry remaining** 跑完剩余页或直接合成下载部分结果。
- **调试日志**：后端全链路 logging（`backend/ocr_config.toml` 的 `log_level` 控制级别，
  默认 INFO，设 DEBUG 看详细）。WebUI 右上角 **Logs** 按钮可实时查看后端日志，
  或调用 `GET /api/logs`。
- **临时文件清理**：任务状态**持久化**在 `work/<job_id>/job.json` 并在启动时自动恢复，
  重启不再丢任务（运行中崩溃的任务恢复为 stopped；hOCR 工作文件夹保留，
  已识别页可直接合成）。清理只针对**无任务引用**且超过 `cleanup_max_age_hours`
  （默认 168h=7 天）的孤儿文件；清理间隔 `cleanup_interval_hours`（默认 6h），
  **被任务引用的文件永不删除**。WebUI 右上角 **Cleanup** 按钮可查看概况并手动清理。
- **批量上传 / ZIP 打包**：多文件上传各自成任务（无队列管理器）；ZIP 成员按源文件名
  命名、重名自动加 ` (2)` 序号；打包只包含**已嵌入**且文件仍在磁盘上的任务，其余跳过，
  全部无结果时返回 404。临时 ZIP 写在系统临时目录、随响应流式返回，发送完成后由
  后台任务删除。
- 运行时产物（`output/`、`work/`、`uploads/`、`backend/ocr_config.toml`）均不应提交仓库。
