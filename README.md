# PDF OCR Embed

跨平台的纯图片 PDF → 内嵌可搜索/可选中文字层工具。上传扫描 PDF，OCR 识别文字，
按识别坐标把文字**不可见地嵌入** PDF 内层（搜索、复制、选择可用，视觉不叠加），
同时提供 WebUI 编辑识别结果（修正错字）与文本框高亮预览。

本项目是**完全独立的程序**：不依赖 OpenClaw，不在代码里硬编码任何 API key。

---

## 特性

- **通用 OCR 抽象（Adapter 模式）**：后端统一走 `OcrSource` 接口，每个 OCR 引擎
  一个 adapter，把各自的原始输出解析为归一化的 `OcrPage`（bbox 统一为**原始像素坐标**）。
  - `unlimited_ocr_adapter`（完整实现，默认）：解析 `<|det|>type [bbox]<|/det|>content`
    标记，并把 1000×1000 归一化画布 bbox 映射回真实像素坐标（逐维缩放）。
  - `tesseract_adapter`（**完整实现**）：本地 Tesseract OCR，无 API key。用
    pytesseract 读取词级 TSV，把词按行聚合成块（每行一个 block），自动划分
    text/heading/equation，中文需安装对应语言包（如 `chi_sim`）。
  - `generic_openai_adapter`（**完整实现**）：接任意 OpenAI 兼容视觉模型，用提示词
    让模型返回带 bbox 的结构化 JSON，同样映射回像素坐标。
- **OCR 设置完全外部化**：环境变量 / 本地 `ocr_config.json` / WebUI 设置页。
  配置优先级：**环境变量 > 本地配置文件 > WebUI 保存值**。
- **PDF 处理**：PyMuPDF 每页转图 → 逐页 OCR → 用 `render_mode=3` 不可见嵌入文字，
  正确处理像素坐标 → PDF 坐标（y 轴翻转），保存为 `*_embedded.pdf`。
- **进度流**：SSE 推送每页 OCR 进度。
- **并行 OCR**：支持并指定并行数，多页并发调用 OCR 引擎（线程池），显著加快多页文档处理。
- **单页 WebUI**：左侧可编辑文本块，右侧页面预览 + bbox 高亮框，设置表单、嵌入按钮、进度条、
  并行数输入框。
- **无 CUDA / NVIDIA** 依赖。

---

## 安装

```bash
cd /home/david/vibe-arena/pdf-ocr-embed
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置 OCR（三选一，优先级从前到后）

### 1) 环境变量

```bash
export OCR_API_KEY="你的 key"                 # 或 USTC_API_KEY 别名
export OCR_BASE_URL="https://api.llm.ustc.edu.cn/v1"   # 任意 OpenAI 兼容端点
export OCR_MODEL="glm-4v-flash"
export OCR_PROVIDER="ustc"                    # 可选：ustc | openai | custom
```

任意 OpenAI 兼容端点均可，只需改 `OCR_BASE_URL` + `OCR_MODEL` 即可切换引擎。

### 2) 本地配置文件（建议放入 .gitignore）

在 `backend/` 下创建 `ocr_config.json`：

```json
{
  "provider": "ustc",
  "api_key": "你的 key",
  "base_url": "https://api.llm.ustc.edu.cn/v1",
  "model": "glm-4v-flash"
}
```

或 `backend/.env`（格式 `KEY=value`）。

### 3) WebUI 设置页

启动后在网页右上角 **Settings** 填写保存（key 会被打码存储）。

> 未配置任何 key 时调用 OCR 会返回明确错误提示，其它功能（上传、预览）不受影响。

---

## 启动

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
# 或
python -m backend.main
```

浏览器打开 <http://localhost:8000>，拖入 PDF 即可。

### 选择 OCR 引擎

WebUI 上传区可下拉选择三个引擎：

- **Unlimited OCR (API)**（默认）— 需配置 API key / base_url / model（见上文）。
- **Tesseract (local)** — 本地 OCR，**无需 API key**。在"Tesseract language"
  填语言包，如 `chi_sim`（中文）、`eng`（英文）、`chi_sim+eng`（中英混合）。
- **Generic OpenAI (API)** — 接任意 OpenAI 兼容视觉模型，key 走 Settings。

命令行方式（tesseract 示例）：

```bash
export OCR_TESS_LANG=chi_sim   # 或写到 backend/ocr_config.json 的 "tess_lang"
uvicorn backend.main:app --port 8000
```

### API 速览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET  | `/` | WebUI 页面 |
| GET  | `/api/health` | 健康检查 + 可用 adapter |
| GET/POST | `/api/settings` | 读取 / 保存 provider 配置（打码） |
| POST | `/api/ocr/upload` | 上传 PDF → 后台逐页 OCR（支持 `concurrency` 并行数，`adapter` 引擎选择，`lang/psm/oem` 供 tesseract，`base_url/api_key/model` 供 API 类）→ 返回 job id |
| POST | `/api/ocr/retry/{job_id}` | 对已上传但失败/中断的任务重跑 OCR（只跑缺失页，不重头开始；参数同 upload） |
| POST | `/api/ocr/stop/{job_id}` | 中途停止正在运行的 OCR（已完成页保留，可下载或重试剩余） |
| GET  | `/api/logs` | 获取最近后端调试日志 |
| GET  | `/api/ocr/stream/{job_id}` | SSE 进度流 |
| GET  | `/api/pages/{job_id}` | 取全部分页 OCR 数据 |
| GET  | `/api/pages/{job_id}/{i}/image` | 页面预览 PNG |
| POST | `/api/pages/{job_id}/{i}` | 更新单个可编辑页 |
| POST | `/api/embed/{job_id}` | 嵌入（可编辑后的）文字 → `*_embedded.pdf` |
| GET  | `/api/download/{job_id}.pdf` | 下载嵌入结果 |

---

## 目录结构

```
pdf-ocr-embed/
├── backend/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 应用与全部路由
│   ├── config.py               # 外部设置解析（env / 配置文件 / WebUI）
│   ├── models.py               # 归一化 OcrPage / OcrBlock 结构
│   ├── pdf_processing.py       # PyMuPDF 转图 + 不可见文字嵌入
│   ├── ocr_service.py          # OCR 编排 + 任务/进度状态
│   └── sources/
│       ├── __init__.py
│       ├── base.py             # OcrSource 抽象基类 + 坐标换算工具
│       ├── factory.py          # adapter 注册/获取
│       ├── unlimited_ocr_adapter.py   # 完整实现（<|det|> 标记解析）
│       ├── tesseract_adapter.py       # stub
│       └── generic_openai_adapter.py  # stub
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── requirements.txt
├── .env.example
├── .gitignore
└── DESIGN.md
```

---

## 说明与限制

- bbox 为 `[x1,y1,x2,y2]` 整数，adapter 内部把归一化画布换算为真实像素，前端/嵌入统一用像素坐标。
- `max_tokens` 已设为 16384（必须 < 32768，否则 API 400）。
- 像素 → PDF 坐标做了 y 轴翻转（PDF 原点左下、像素原点左上），并用页面 rect 与渲染宽高比例缩放。
- **Tesseract adapter（本地，无 key）**：
  - 语言通过 `OCR_TESS_LANG`（或 `ocr_config.json` 的 `tess_lang`、WebUI 上传区）
    配置，中文用 `chi_sim`，可组合 `chi_sim+eng`。
  - 需系统装有 `tesseract` 二进制 + 对应语言包（Fedora：`tesseract` +
    `tesseract-langpack-chi_sim`）。二进制不在 PATH 时用 `OCR_TESS_CMD` 指定，
    tessdata 不在默认位置时用 `OCR_TESSDATA_DIR`。
  - 每行文本聚合成一个 block，自动识别 heading / equation / text，输出置信度。
- **generic_openai adapter（任意 OpenAI 兼容视觉模型）**：与 unlimited 相同
  的 key/base_url/model 配置，`OCR_GENERIC_PROMPT` 可覆盖默认的 bbox-JSON 提示词。
- **并行数（concurrency）**：上传时可指定，WebUI 上传区有输入框，或调用
  `POST /api/ocr/upload` 时带 `concurrency` 表单字段（1–32）。后端用线程池并发处理各页，
  `concurrency=1` 即顺序执行。注意并发越高对 OCR 引擎/API 的并发压力越大，需与引擎配额匹配。
- **失败重试（智能）**：OCR 报错或中途停止后，WebUI 显示 **Retry remaining** 按钮。
  重试**只重跑失败/未完成的页**，已成功的页保留不重跑（修复了"跑了 99% 重试却从头开始"的问题）。
  也可调用 `POST /api/ocr/retry/{job_id}`，复用已上传的 PDF，无需重新上传。
- **中途停止**：OCR 运行中可点击 **Stop** 按钮或调用 `POST /api/ocr/stop/{job_id}` 停止。
  已完成的页保留，停止后可点 **Download partial** 下载部分嵌入的 PDF，或 **Retry remaining** 跑完剩余页。
- **调试日志**：后端全链路 logging（`OCR_LOG_LEVEL` 环境变量控制级别，默认 INFO，设 DEBUG 看详细）。
  WebUI 右上角 **Logs** 按钮可实时查看后端日志，或调用 `GET /api/logs`。
- 运行时产物（`output/`、`work/`、`uploads/`、`ocr_config.json`、`.env`）均不应提交仓库。