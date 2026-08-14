# PDF OCR Embed — 跨平台 GUI/WebUI：纯图片 PDF 嵌入文字

## 目标
做一个跨平台的桌面 GUI / WebUI 程序：输入纯图片 PDF（扫描件），通过 OCR 识别的文字
按坐标**嵌入**到 PDF 内层（真正的可选中/可搜索文字层），保留原图作为背景。

> **Vibe coding 提示**：本文档与整个项目均由 **DeepSeek V4 Flash** 通过 Vibe coding
> 生成，属设计思路记录而非权威规范，实现以其对应代码为准。审阅代码时勿盲信 AI 输出。

## 独立运行
- API key 与 provider 配置通过**外部方式**提供，代码里绝不硬编码：
  - 环境变量：`OCR_API_KEY`、`OCR_BASE_URL`、`OCR_MODEL`（可选 `OCR_PROVIDER`）。
  - 或项目本地配置文件 `ocr_config.json` / `.env`（用户自建，加入 .gitignore）。
  - 或 WebUI 设置页里填写保存。
- 配置优先级：CLI/环境变量 > 本地配置文件 > WebUI 默认值。
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
- `POST /api/settings` 保存/读取 provider 配置（key 打码，不落明文偏好；或存本地）。
- `POST /api/ocr/upload` 上传 PDF → 转图 → 逐页 OCR → 返回页级 JSON；
  长任务用 SSE `/api/ocr/stream` 推进度。
- `GET /api/pages/{i}/image` 拿页面预览图。
- `POST /api/embed` 接收（可编辑后的）OcrPage 列表 → 生成嵌入 PDF。
- `GET /api/download/{job_id}.pdf` 下载结果。

## 前端
- 单页 WebApp（原生 JS / Vue 简洁优先）。
- 左侧评论区：可编辑每块的文本；右侧实时预览 PDF 页 + 文本框高亮框。
- 保存 / 嵌入按钮。

## 质量要求
- 代码可运行，README 写清依赖、配置方式（外部 key / provider）与启动命令。
- 后端统一走 OcrSource 抽象，不能只写死 Unlimited-OCR。
- 不引入 CUDA / NVIDIA 依赖（部署节点无 NVIDIA GPU）。

## 交付
在 /home/david/vibe-arena/pdf-ocr-embed 下写完整个项目 + README。
