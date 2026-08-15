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

- **通用 OCR 抽象（Adapter 模式）**：后端统一走 `OcrSource` 接口，每个 OCR 引擎
  一个 adapter，把各自的原始输出解析为归一化的 `OcrPage`（bbox 统一为**原始像素坐标**）。
  - `unlimited_ocr_adapter`（完整实现，默认）：解析 `<|det|>type [bbox]<|/det|>content`
    标记，并把 1000×1000 归一化画布 bbox 映射回真实像素坐标（逐维缩放）。
  - `tesseract_adapter`（**完整实现**）：本地 Tesseract OCR，无 API key。用
    pytesseract 读取词级 TSV，把词按行聚合成块（每行一个 block），自动划分
    text/heading/equation，中文需安装对应语言包（如 `chi_sim`）。
  - `generic_openai_adapter`（**完整实现**）：接任意 OpenAI 兼容视觉模型，用提示词
    让模型返回带 bbox 的结构化 JSON，同样映射回像素坐标。
- **OCR 设置完全外部化**：本地 TOML 配置 `backend/ocr_config.toml` + WebUI 设置页
  （WebUI 保存时写入同一个 TOML 文件）。`OCR_*` **环境变量可选地覆盖**全部设置
  （优先级最高：环境变量 > WebUI 会话内保存值 > TOML 文件）；JSON / .env 文件
  配置已移除。
- **PDF 处理**：PyMuPDF 每页转图 → 逐页 OCR → 用 `render_mode=3` 不可见嵌入文字，
  正确处理像素坐标 → PDF 坐标（y 轴翻转），保存为 `*_embedded.pdf`。
- **进度流**：SSE 推送每页 OCR 进度。
- **并行 OCR**：支持并指定并行数，多页并发调用 OCR 引擎（线程池），显著加快多页文档处理。
- **单页 WebUI**：左侧可编辑文本块，右侧页面预览 + bbox 高亮框，设置表单、嵌入按钮、进度条、
  并行数输入框。
- **国际化（i18n）**：内置**英文 / 中文**两套界面，页头可随时切换（`frontend/i18n.js`），
  默认跟随浏览器语言；切换语言不刷新页面即时生效。
- **浅色 / 深色 / 自适应主题**：页头切换，选择记忆在 localStorage；自适应跟随系统
  `prefers-color-scheme`，深色下原生控件与滚动条同步变暗。
- **WebUI 体验优化**：Toast 通知、`Ctrl/⌘+Enter` 快速嵌入、`←/→` 翻页快捷键、
  主题/引擎/字号等偏好本地记忆、焦点可见样式与 `prefers-reduced-motion` 支持、
  内嵌 SVG favicon 与随主题变化的 `theme-color`。
- **无 CUDA / NVIDIA** 依赖。

---

## 安装

```bash
cd /home/david/vibe-arena/pdf-ocr-embed
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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
| `OCR_TESS_LANG` | `tess_lang` |
| `OCR_TESS_PSM` | `tess_psm` |
| `OCR_TESS_OEM` | `tess_oem` |
| `OCR_TESS_CONFIG` | `tess_config` |
| `OCR_TESSDATA_DIR` | `tessdata_dir` |
| `OCR_TESS_CMD` | `tess_cmd` |
| `OCR_GENERIC_PROMPT` | `generic_prompt` |
| `OCR_EMBED_FONT` | `embed_font` |
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

WebUI 上传区可下拉选择三个引擎：

- **Unlimited OCR (API)**（默认）— 需配置 API key / base_url / model（见上文）。
- **Tesseract (local)** — 本地 OCR，**无需 API key**。在"Tesseract language"
  填语言包，如 `chi_sim`（中文）、`eng`（英文）、`chi_sim+eng`（中英混合）。
- **Generic OpenAI (API)** — 接任意 OpenAI 兼容视觉模型，key 走 Settings。

命令行方式（tesseract 示例）：

```bash
# 把 chi_sim 写进 backend/ocr_config.toml（或 WebUI 上传区的 "Tesseract language"）
echo 'tess_lang = "chi_sim"' >> backend/ocr_config.toml
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
| GET  | `/api/cleanup` | 临时文件清理概况（未被任务引用的 work/output/uploads 文件数量与大小） |
| POST | `/api/cleanup/run` | 执行/预览清理（`older_than_hours` 保留时长、`dry_run` 预览、`force` 忽略时限，仍永不删任务在用文件） |
| GET  | `/api/cache` | OCR 结果缓存状态（条目数/字节/命中与未命中计数、TTL、开关） |
| POST | `/api/cache/clear` | 清空全部 OCR 缓存（不影响任务内已识别的结果） |

---

## 目录结构

```
pdf-ocr-embed/
├── backend/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 应用与全部路由
│   ├── config.py               # 外部设置解析（TOML 配置文件 / WebUI）
│   ├── models.py               # 归一化 OcrPage / OcrBlock 结构
│   ├── pdf_processing.py       # PyMuPDF 转图 + 不可见文字嵌入
│   ├── ocr_service.py          # OCR 编排 + 任务/进度状态
│   └── sources/
│       ├── __init__.py
│       ├── base.py             # OcrSource 抽象基类 + 坐标换算工具
│       ├── factory.py          # adapter 注册/获取
│       ├── unlimited_ocr_adapter.py   # 完整实现（<|det|> 标记解析）
│       ├── tesseract_adapter.py       # 完整实现（本地 Tesseract）
│       └── generic_openai_adapter.py  # 完整实现（任意 OpenAI 兼容视觉模型）
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── i18n.js                 # 英文 / 中文双语界面
├── tests/                      # pytest 测试（坐标映射/解析器/缓存等纯函数）
├── requirements-dev.txt        # 开发依赖（pytest）
├── requirements.txt
├── config.example.toml
├── .gitignore
├── AGENTS.md     # 面向 AI 编码代理的项目指南（含如何编写 OCR adapter）
├── DESIGN.md
└── FEATURE_IDEAS.md  # 新功能脑暴清单（候选 roadmap，非排期承诺）
```

---

## 说明与限制

- bbox 为 `[x1,y1,x2,y2]` 整数，adapter 内部把归一化画布换算为真实像素，前端/嵌入统一用像素坐标。
- `max_tokens` 已设为 16384（必须 < 32768，否则 API 400）。
- 像素 → PDF 坐标做了 y 轴翻转（PDF 原点左下、像素原点左上），并用页面 rect 与渲染宽高比例缩放。
- **Tesseract adapter（本地，无 key）**：
  - 语言通过 `backend/ocr_config.toml` 的 `tess_lang`（或 WebUI 上传区）配置，
    中文用 `chi_sim`，可组合 `chi_sim+eng`。
  - 需系统装有 `tesseract` 二进制 + 对应语言包（Fedora：`tesseract` +
    `tesseract-langpack-chi_sim`）。二进制不在 PATH 时用 `tess_cmd` 指定，
    tessdata 不在默认位置时用 `tessdata_dir`。
  - 每行文本聚合成一个 block，自动识别 heading / equation / text，输出置信度。
- **generic_openai adapter（任意 OpenAI 兼容视觉模型）**：与 unlimited 相同
   的 api_key/base_url/model 配置，`generic_prompt` 可覆盖默认的 bbox-JSON 提示词。
- **并行数（concurrency）**：上传时可指定，WebUI 上传区有输入框，或调用
  `POST /api/ocr/upload` 时带 `concurrency` 表单字段（1–32）。后端用线程池并发处理各页，
  `concurrency=1` 即顺序执行。注意并发越高对 OCR 引擎/API 的并发压力越大，需与引擎配额匹配。
- **失败重试（智能）**：OCR 报错或中途停止后，WebUI 显示 **Retry remaining** 按钮。
  重试**只重跑失败/未完成的页**，已成功的页保留不重跑（修复了"跑了 99% 重试却从头开始"的问题）。
  也可调用 `POST /api/ocr/retry/{job_id}`，复用已上传的 PDF，无需重新上传。
- **中途停止**：OCR 运行中可点击 **Stop** 按钮或调用 `POST /api/ocr/stop/{job_id}` 停止。
  已完成的页保留，停止后可点 **Download partial** 下载部分嵌入的 PDF，或 **Retry remaining** 跑完剩余页。
- **OCR 结果缓存**：同一 PDF 页 + 相同引擎与参数的结果，按内容哈希缓存到
  `cache/ocr/`（键 = 源 PDF 哈希 + 页码 + 渲染参数 + 引擎指纹，绝不落盘任何密钥或
  原图）。重复 OCR 直接命中，省时省钱、且错误的缓存条目会被自动丢弃。TTL
  `ocr_cache_max_age_hours`（默认 720h），`ocr_cache_enabled = false` 可整体关闭；
  后台清理循环会顺带过期清理，`GET /api/cache` 看命中统计、`POST /api/cache/clear`
  一键清空。缓存只存**识别原文**，用户在页面上做的文字/字体修改完全不受影响。
  **命中即跳过渲染**：重新上传同一文档时，缓存命中的页不需要重新渲染 PNG
  （预览图在首次查看时按需补渲染）。SSE 进度分 `render`（预处理渲染）与
  `ocr`（识别）两个阶段推送，处理大文件时进度条在渲染阶段就持续前进。
- **调试日志**：后端全链路 logging（`backend/ocr_config.toml` 的 `log_level` 控制级别，
  默认 INFO，设 DEBUG 看详细）。WebUI 右上角 **Logs** 按钮可实时查看后端日志，
  或调用 `GET /api/logs`。
- **临时文件清理**：任务只在内存中保存——服务重启后 `work/<job_id>/`（源 PDF + 每页渲染图）
  与 `output/`（嵌入结果、缩略图、overlay）会变成无人引用的孤儿文件，长期堆积占用磁盘。
  后端在**启动时**与每 `cleanup_interval_hours`（默认 6h）自动删除
  未被任何存活任务引用、且超过 `cleanup_max_age_hours`（默认 168h=7 天）的临时文件
  （两个值都在 `backend/ocr_config.toml` 中配置）；
  **被任务引用的文件永不删除**。WebUI 右上角 **Cleanup** 按钮可查看概况、调整保留时长并手动
  清理（Preview 先预览、Clean now 执行），也可直接调 `/api/cleanup` 与 `/api/cleanup/run`。
- 运行时产物（`output/`、`work/`、`uploads/`、`backend/ocr_config.toml`）均不应提交仓库。
