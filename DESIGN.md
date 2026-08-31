# PDF OCR Embed — 跨平台 GUI/WebUI：纯图片 PDF 嵌入文字

## 目标
做一个跨平台的桌面 GUI / WebUI 程序：输入纯图片 PDF（扫描件），通过 OCR 识别的文字
按坐标**嵌入**到 PDF 内层（真正的可选中/可搜索文字层），保留原图作为背景。

> **Vibe coding 提示**：本文档与整个项目均由 **DeepSeek V4 Flash** 通过 Vibe coding
> 生成，属设计思路记录而非权威规范，实现以其对应代码为准。审阅代码时勿盲信 AI 输出。

## 独立运行
- API key 与 provider 配置通过**外部方式**提供，代码里绝不硬编码：
  - 项目本地 TOML 配置文件 `backend/ocr_config.toml`（由根目录 `config.example.toml`
    复制而来，已加入 .gitignore）。
  - 或 WebUI 设置页里填写保存（保存写入同一个 TOML 文件）。
- 旧版 `OCR_*` 环境变量继续支持，作为**最高优先级覆盖**：
  `环境变量 > WebUI 会话内值 > TOML 文件`；JSON / `.env` 文件配置已移除。
- 默认只给 USTC 的 OpenAI 兼容端点示例（`https://api.llm.ustc.edu.cn/v1`），
  但必须支持任意 OpenAI 兼容端点（通过 base_url + api_key + model 配置即可切引擎）。

## 技术栈建议
- 后端：Python（FastAPI 提供 REST + 文件上传 + 进度事件）。
- 前端：单页 WebUI（浏览器即用），可选后续用 Tauri 打包桌面。
- 不引入 CUDA / NVIDIA 依赖。

## OCR 输出格式 — 通用抽象（核心设计）

现有 Unlimited-OCR（百度/USTC API）输出带 `<|det|>` 标记，格式如下：

```text
<|det|>title [50,100,200,120]<|/det|>Document Title
<|det|>text [50,150,300,170]<|/det|>Paragraph content here.
<|det|>image [300,200,500,400]<|/det|>
<|det|>image_caption [..]<|/det|>Figure 5. ...
```

要点（经验证）：
1. bbox 是 [x1,y1,x2,y2] 整数。
2. **bbox 不是原始像素坐标**！模型把每张图归一化到固定 1000×1000 画布，
   每维独立缩放到 1000（非等比）。映射回原图：
   `real_x = bbox_x * (img_width/1000)`，`real_y = bbox_y * (img_height/1000)`。
3. marker 行格式：`<|det|>type [bbox]<|/det|>content`，可选 bbox 和 content。
4. image 区域无 content，caption 在后续的 `image_caption` 块里。
5. max_tokens 必须 < 32768，否则 API 400。

### 归一化的内部 Schema（通用，不只绑死 Unlimited-OCR）

设计一个中间数据结构 `OcrPage`，解析任意 OCR 原始输出，统一成：

```json
{
  "page_index": 0,
  "width": 1654,
  "height": 2339,
  "blocks": [
    {"kind": "text", "bbox": [x1,y1,x2,y2], "text": "...", "conf": 0.98},
    {"kind": "heading", "bbox": [...], "text": "..."},
    {"kind": "equation", "bbox": [...], "text": "..."},
    {"kind": "table", "bbox": [...], "text": "..."},
    {"kind": "image", "bbox": [...], "caption": "Figure 5 ..."},
    {"kind": "footnote", "bbox": [...], "text": "..."}
  ]
}
```

- **Adapter 模式**：每个 OCR 引擎一个 adapter，把原始输出解析成 `OcrPage`。
  - `unlimited_ocr_adapter`：解析 `<|det|>` 标记（默认）。
  - 预留接口：`tesseract_adapter` / `paddle_adapter` / `generic_openai_adapter`
    （把任意 OpenAI 兼容多模态模型的输出按 bbox 规范解析）。
  - **编写新 adapter 的完整步骤与检查清单见 `AGENTS.md`（面向 AI 代理的权威指南）。**
- bbox 坐标统一转换为**原始像素空间**（adapter 内完成 1000 画布 → 像素换算，
  换算所需原图宽高由调用方传入）。

## PDF 嵌入文字（Invisible Text / 可搜索层）

用 PyMuPDF (fitz)：
- `page.insert_text(point, text, fontsize=..., render_mode=3)` render_mode=3 表示
  仅渲染到文本提取层、不可见（搜索/复制可用，视觉不叠加）。
- 每页插完保存为 `*_embedded.pdf`。
- bbox 像素坐标 → PDF 页面坐标：PDF 原点左下、y 轴向上；像素原点左上。
  `pdf_y = page_height_pdf - bbox_y`，用 page rect 与像素宽高比例缩放。

## 功能
1. 上传 PDF（或拖拽多页）。
2. 每页转图（PyMuPDF / pdftoppm），调 OCR。
3. 展示识别结果（可编辑文本块，修正错字）。
4. 一键嵌入文字 → 生成 `_embedded.pdf`。
5. 进度条（SSE / WebSocket 每页进度事件）。

## API 设计（FastAPI）
- `POST /api/settings` 保存/读取 provider 配置（key 打码显示；保存时写入 `backend/ocr_config.toml`）。
- `POST /api/ocr/upload` 上传 PDF → 转图 → 逐页 OCR → 返回页级 JSON；
  长任务用 SSE `/api/ocr/stream` 推进度。
- `GET /api/pages/{i}/image` 拿页面预览图。
- `POST /api/embed` 接收（可编辑后的）OcrPage 列表 → 生成嵌入 PDF。
- `GET /api/download/{job_id}.pdf` 下载结果。

## 执行入口与可靠性
- Web：`backend/main.py`（FastAPI）；无头 CLI：`backend/cli.py`
  （`python -m backend.cli`），与 Web 共用 `ocr_service` / `pdf_processing` /
  `config.resolve()` 同一套后端路径，无服务器也可批量处理。
- HTTP adapter 的引擎调用走 `backend/sources/http_utils.py`：
  429/5xx/瞬时网络错误按指数退避重试（尊重 `Retry-After`），并提供线程安全的
  per-adapter 限速（`rate_limit_rps`）。
- `POST /api/ocr/retry/{job_id}` 支持 `page_start` / `page_end` / `force`
  （页范围 + 强制重跑已成功页，A/B 试跑用）；页选择逻辑集中在
  `ocr_service.select_pages()`（纯函数，含单元测试）。
- OCR 输入图像预处理（#2）：`pdf_processing.preprocess_image()` 在新渲染的
  页面 PNG 上执行 PIL 清洗（灰度 / 中值去噪 / autcontrast / Otsu 二值化），
  由 `preprocess_*` 配置开关控制。**尺寸不变**是硬约束——预处理前后宽高完全
  一致，因此块 bbox 像素坐标语义永不改变；预处理只发生在 OCR 输入的渲染
  路径（含按需预览补渲染），命中缓存的页面不渲染、不预处理。
- 嵌后校验（#17）：`backend/validation.py` 用 PyMuPDF 抽取嵌入 PDF 的文字层，
  与 OCR 源页逐页比对，输出页级覆盖率（token 重叠 × 字符连续度的几何平均）、
  字数、置信度统计与汇总；比较数学全部为纯函数（可脱离 PDF 单测），
  仅 `build_report()` 触碰 PyMuPDF 且全程只读、出错返回 `ok:false` 而非抛错。
  `POST /api/embed` 响应携带 `report`，另有 `GET /api/validation/{job_id}`
  按需重跑（对存储页校验——浏览器内未重新嵌入的编辑不反映在内，属预期限制）。
- 块操作（#6）：纯前端。`frontend/app.js` 在 `state.sel` 上维护每会话的
  `selection`（当前页选中块索引集合）、`undoStack`（结构编辑前的 blocks 快照）、
  `drawMode` / `pendingDraw`（叠加层绘制新块）；`#overlay-canvas` 捕获
  pointer 事件做移动/缩放/绘制，全部 bbox 变更经 `clampBbox` 夹取为页面内
  **整数像素**坐标并保持 `x1<=x2`、`y1<=y2`。结构性编辑仍走「嵌入时发送整页」
  的既有路径，无后端改动、无持久化（撤销仅会话内）。

## 前端
- 单页 WebApp（原生 JS / Vue 简洁优先）。
- 左侧评论区：可编辑每块的文本；右侧实时预览 PDF 页 + 文本框高亮框。
- 保存 / 嵌入按钮。

## 批量上传 + 队列 + 打包下载（#10）
- 上传区支持一次拖入/选择多个 PDF；**每个文件独立成任务**（独立 id、卡片、SSE、
  持久化），复用现有多任务并行能力，**不做单独的队列管理器**；服务端 `/api/jobs`
  始终是任务列表唯一数据源，前端并发发出所有上传后直接回拉列表并各自订阅进度。
- `POST /api/ocr/upload` 同时接受旧字段 `file`（单个，向后兼容）与新字段 `files`
  （可多个，按对象身份去重）；多文件时返回 `{"jobs": [{job_id, filename, status}, ...]}`，
  单文件时仍带顶层 `job_id` / `filename` / `status`（老前端不破坏）。
- `GET /api/ocr/zip?jobs=id1,id2,...`：把所选任务的**嵌入式输出 PDF**
  （`job["embedded_path"]`）按源文件名打包成一个 ZIP。打包逻辑集中在
  `backend/batch.py`（纯函数、可单测）：`collect_embedded` 只保留确有嵌入结果的
  任务，`build_zip` 用 `zipfile` + `copyfileobj` 把每个成员**分块从磁盘流式写入**
  （ZIP 不整包进内存），重名成员自动加序号；全部无结果时返回 404。临时 ZIP 放
  系统临时目录，以 `FileResponse` 流式返回，响应发送完成后由后台任务删除。

## 质量要求
- 代码可运行，README 写清依赖、配置方式（外部 key / provider）与启动命令。
- 后端统一走 OcrSource 抽象，不能只写死 Unlimited-OCR。
- 不引入 CUDA / NVIDIA 依赖（部署节点无 NVIDIA GPU）。

## 路线图

当前阶段：**修复现存小 Bug，打磨稳定性**。大量基础功能已落地
（并行 OCR、批量上传、任务持久化、嵌后校验、CLI、缓存、重试限速等），
先把它们打磨到稳定，再拓展功能。

1. **第一个稳定版（当前焦点）**：修复已知小 Bug，补齐测试与文档，
   打 tag 发布 v1.0.0。
2. **稳定版之后的扩展方向**：让 OCR 的**中间结果**（归一化 `OcrPage` /
   `OcrBlock`，含块 bbox、kind、置信度）不再只服务于「嵌入隐形文字层」，
   而是成为可复用的产物，例如：
   - **还原可编辑 PDF**：按块结构重建原生文字 PDF（真文本而非隐形层）。
   - **导出 Markdown / LaTeX**：利用 `kind=heading/equation/table` 与阅读序，
     生成结构化文档导出（table→markdown 表格、equation→LaTeX）。
   - 其他下游用途（纯文本导出、检索索引等）自然受益于同一套中间结果。

## 交付
项目已完成并持续维护于本仓库（`pdf-ocr-embed`）；文档与测试随功能同步更新。
