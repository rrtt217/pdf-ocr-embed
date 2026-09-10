# RESEARCH — 导出 markdown/LaTeX 前用 LLM 预处理

分支 `research/export-llm-preprocess`（基于 main @ 2756132）· 研究文档，未接线路由

---

## 0. TL;DR

1. **落点选导出期，不选 OCR 期**：LLM 只改「给导出用的 pages 副本」，sidecar / hOCR /
   嵌入文本层零接触 —— 不碰任何坐标与文本层硬不变式。
2. **形态选「确定性预处理 + 按需 LLM 修整」**：新纯函数预处理（默认开、离线可测）解决
   80% 的格式问题；LLM（默认关，`?llm=1` / `--export-llm`）只处理被标记的难块
   （表格 / 公式 / 低置信 / 跨页衔接）。
3. **架构上 LLM 是 pages→pages 的变换**：`export.py` 的纯函数 builder 一行不改，
   只把 `block_source` 优先级扩为 `llm > raw > text`。可单测、可缓存、可回退。
4. **安全网**：JSON 结构化输出 + 长度比 guard + 解析失败重试一次 + 任何 LLM 故障
   静默回退 —— 导出永不因 LLM 失败而失败。
5. **成本**：块级模式通常只触发全文档 10–20% 的块；整文档重排最贵（约等于重新识别
   一遍的输出量）且幻觉风险最高，只作实验模式。

> **状态（P1/P2 + WebUI 已实现，分支本 worktree）**：`backend/export_llm.py`（P0 确定性
> reflow + P1 LLM 块修整 + progress 回调）、`backend/export.py` 的 `llm > raw > text`
> 三层 `block_source` 与 `_md_table_to_rows`、config 键 + env 别名、`?llm=1`/`?reflow=0`
> 路由参数 + SSE 导出流（`/api/export/stream/{job}.{ext}`：progress 事件 + done 携带全文、
> 15s keepalive）、`--export-llm`/`--no-reflow` CLI、WebUI 导出按钮 + 独立导出进度条、
> `tests/test_export_llm.py`（80 例）。
> 关键洞见落地：**line-split 对导出是反模式**（见 §1），P0 的第一步就是把它 unwrap 掉。
>
> **后续加固（使 LLM 章节/块修整在实际模型上稳定）**：
> - 大纲精修改为**先行于块修整**（章节是 1–2 个小请求，不能让位给上百个块请求）；
>   标题过多时按 ≤60 个/请求分段，单调加深守卫跨块保持，坏块只回退该块。
> - 响应 JSON 容错提取（围栏 / 散文 / 思考块 / 尾随文字）；坏 JSON 自动重试一次。
> - 兼容思考型模型：`content` 缺失时回退 `reasoning_content`；`finish_reason=length`
>   截断时加大预算重试；默认 `export_llm_timeout_s` 120 → 240。
> - **结构化输出**：实测服务器 `qwen3.8-chat` 支持 `response_format: {"type": "json_object"}`
>   （json_object 2–3s / json_schema 13–31s 均有效；无约束基准偶发 150s+ 超时）。
>   `ExportLlmClient.chat_json` 默认开启 json 模式，网关 400/422 拒收时自动降级重试。
> - 目录检测窗口 5 → 30 页（真实书籍的目录常在 5+ 页之后），命中后连续 2 页无目录即停。
> - 图片导出走 SSE 流并报告「提取图片 / 打包」阶段进度；`split=1` 按章节拆分 md 打包 ZIP，
>   多文件结果经一次性下载链接 `/api/export/download/<token>` 交付。

---

## 1. 现状与问题清单

现管线（`backend/main.py::export_document` → `backend/export.py`）：

```
GET /api/export/{job}.md|.tex?raw=1
  → export.load_job_pages(job)          # ocr_service.get_pages → block dicts
  → export.export_document(fmt, pages)  # 纯函数：块 → md/latex
```

块字段：`kind / bbox / text / lines / conf / raw / caption`。`raw` 是
`generate_raw` 开启时引擎写入的预归一化内容（表格 HTML、LaTeX 源）。

| # | 质量缺口 | 现状 | LLM 能否帮 |
|---|----------|------|-----------|
| 1 | 表格：tesseract 路径无 raw HTML，只有归一化 tab 行，列易错位 | `_tab_text_to_rows` 直接切 tab | ✅ 重建表格（最好配页图取证） |
| 2 | 公式：无 raw 时归一化已把 LaTeX 打平成 plain（`\frac` 丢失） | `_math_body` 原样放进 `$$` | ⚠️ 纯文本反推 LaTeX 幻觉风险高，需页图 crop |
| 3 | 标题层级：无编号标题一律 level 2，层级不看全文 | `heading_level` 编号启发式 | ✅ 全文大纲重排（整文档模式才有效） |
| 4 | 跨页段落：页尾无句读 + 页首续行不会合并 | 直接拼接 | ✅ 确定性启发式可先做，LLM 兜底 |
| 5 | 列表：`1.` / `•` / `-` 开头的块导出为普通段落 | 无列表语法 | ✅ 确定性正则可做大部分 |
| 6 | 断字连字符：`informa-\ntion` 保留在导出里 | 无处理 | ✅ 确定性可完全解决 |
| 7 | OCR 错字：低置信块（conf < 阈值）原样导出 | 无处理 | ✅ 即 FEATURE_IDEAS #3，需页图取证 |
| 8 | 页眉页脚等家具 | `_FURNITURE_KINDS` 已过滤 | ❌ 不需要 LLM |

注意：**导出路径没有文本层不变式**。`text_norm.py` 的那些「绝不合并相邻词」规则
是为嵌入文本层设计的；导出文本可以（也应该）做得更激进。

**核心洞见：line-split 对 markdown/LaTeX 是反模式。** sidecar 块里的硬换行结构
（每行一个 `<br>` 感）是为 PDF 嵌入设计的——fpdf2 渲染器要按行放文本层；
导出文本里它们是噪声：markdown 渲染器把单个换行当软换行（视觉上粘连），
段落断行破坏 latex 源可读性，跨页断行把一段劈成两半。所以 P0 的第一步是
unwrap（CJK 直接拼、拉丁文补空格拼、`词-`+`续`连字符合并），只在
列表/表格/标题/冒号标签行处停笔（见 §5.2）。

---

## 2. 关键架构判断：LLM 做成 pages→pages 变换

`export.py` 全部纯函数（string in / string out）是它能被测试钉住的原因。
因此 LLM 预处理**不进 builder**，而是 builder 之前的一个独立 pass：

```python
pages = export.load_job_pages(job)
pages2 = export_llm.preprocess(pages, cfg, fmt="markdown")   # 深拷贝后改写
text = export.export_document(fmt, pages2, title=..., use_raw=True)
```

`preprocess` 返回深拷贝（绝不改原 pages），给改写过的块打标：

```python
block["llm"] = {"text": "...", "model": "...", "prompt_v": 1}
```

配套把 `export.block_source` 优先级扩一档（3 行 diff，builder 其余不动）：

```python
def block_source(block, use_raw=True):
    if use_raw:
        llm = ((block.get("llm") or {}).get("text") or "").strip()
        if llm:
            return llm
        raw = (block.get("raw") or "").strip()
        if raw:
            return raw
    return (block.get("text") or "").strip()
```

收益：现有格式与测试全部不动；LLM 输出可缓存、可逐块回退、可 A/B 对比
（同一次 OCR 用 `?llm=0/1` 各导一份 diff）。

---

## 3. 业界做法（调研结论）

- **olmOCR（allenai）**：把 PDF 原有文本层抽出来当「anchor text」塞进 VLM prompt，
  让模型以已有文本为 ground truth 去修格式而不是凭空转写 —— 显著降低幻觉、保持阅读序。
  （[anchor.py](https://gitcode.com/daily_hot/olmocr/blob/2ab7cb280c18d9b18ee42a693424aab67f262a47/olmocr/prompts/anchor.py)、
  [模型卡](https://huggingface.co/allenai/olmOCR-7B-0225-preview)、
  [论文](https://papers.lunadong.com/paper/13112)）
  → **直接借鉴**：我们的块模式同样「OCR 文本为准，只修格式」，prompt 里写死
  "do not add, drop, or translate content"。
- **MinerU / Docling / marker**：主流开源 PDF→md 管线都是 structure-first
  （布局模型 + 规则），LLM 只在兜底位。→ 印证「确定性预处理优先，LLM 只啃硬骨头」。
  （[生态综述](https://github.com/green-dalii/obsidian-llm-wiki/blob/main/docs/README_ZH-Hant.md)）
- **The Hidden Structure（arXiv 2505.12837）**：对法律文档用 GPT-4o Vision 显式
  生成 markdown 格式，下游理解指标可测提升。
  （[论文](https://ar5iv.labs.arxiv.org/html/2505.12837)）
  → 证明「导出期 LLM 格式化」对 RAG/阅读有真实收益，值得做。
- **Mathpix 等商用**：金标准是「识别即结构化」。我们的 unlimited 引擎在
  `generate_raw` 开启时已经输出表格 HTML / LaTeX 源 —— **导出期要做的是修整，
  不是重建**；重建只留给「没有 raw 的旧 sidecar / tesseract 路径」。

---

## 4. 四种方案对比

| 方案 | 触发范围 | 输入 | 成本 | 幻觉风险 | 失败半径 | 结论 |
|------|---------|------|------|---------|---------|------|
| A 块级修整 | 被标记的块（表格/公式/低conf/跨页续） | 块文本（+可选块 crop 图） | 低（<10–20% 块） | 低（有 ground truth + guard） | 单块回退 | ✅ 主力 |
| B 页级重排 | 每页全部块 | 块列表（+页图） | 中（每页 1 次） | 中 | 单页回退 | 备选，块多时省调用数 |
| C 整文档重排 | 确定性导出后的全文 | markdown 全文 | 高（输出≈重新识别） | 高（改写自由度大） | 全文回退 | ⚠️ 仅实验，标题层级唯一刚需 |
| D 混合 = 确定性 + A | — | — | 低 | 低 | 最小 | ✅ **推荐** |

---

## 5. 推荐设计（方案 D）

### 5.1 数据流

```
load_job_pages
  → [P0] 确定性预处理（新 export_llm，默认开，纯函数）
      连字符合并 / 列表标记 / 表格 ragged 补齐 / 跨页段落启发式合并
  → [P1] LLM 块修整（默认关）：标记块 → 批量(≤K块/请求) → JSON 输出 → guard → 回填 llm 字段
  → export_document（不改）
```

### 5.2 确定性预处理（P0，无 API 依赖，先落地）

| 规则 | 做法 | 验证 |
|------|------|------|
| 连字符 | 块内 `([A-Za-z])-\n([a-z])` → `\1\2` | 纯函数单测 |
| 列表 | `^\s*(\d+[.)、]\|[•●▪○-])\s+` → md 列表项（写入 llm.text 或直接改 text？→ 只打 `llm`，保持可回退） | 单测 |
| 表格 ragged | 短行补 `""` 至最宽行；1 列「表格」降级为段落 | 单测 |
| 跨页合并 | 页 i 最后 content 块为 text 且不以 `。.!?:;」"'` 结尾，页 i+1 首块以小写/续词开头 → 合并（bbox 用页 i 块，标记 `llm.merged_from=[i+1]`） | 单测 |

### 5.3 LLM 块修整（P1）

**触发**（全部满足才发请求）：`export_llm` 开 && 块非家具 && 满足任一：
`kind in (table, equation)`、`conf` 存在且 `< conf_threshold`（默认 0.85）、
跨页合并成功、（raw 存在但解析行数为 0 的表格）。

**请求**：文本模式即可（v1 不发图）；`temperature=0`；`max_tokens` 按
`len(text)*3` 估、上限 32767（对齐插件硬上限惯例）；一次最多 K=8 块/请求，
块间用 `<<<BLOCK n>>>` 分隔，要求逐块返回 JSON：

```json
{"blocks": [{"n": 0, "ok": true,  "text": "| a | b |\n|---|---|\n| 1 | 2 |"},
            {"n": 1, "ok": false, "reason": "not a real table"}]}
```

**Prompt 骨架**（olmOCR 式 ground-truth 约束）：

```
You are fixing OCR export formatting. For each block you get the recognized
text (ground truth). Fix ONLY structure: rebuild tables as markdown pipe
tables, keep equations as LaTeX, merge hyphenation, keep every fact, number,
name and language EXACTLY as given. Never add, drop, or translate content.
If unsure, set ok=false. Respond with JSON only.
```

**可选图像取证（P3，`export_llm_images=on`）**：对 table/equation 块用
`backend/pdf_processing.py` 现成的页渲染 + 块 bbox 裁图（唯一涉及坐标的地方，
但只裁剪、不写回坐标），随文本一起发给 VLM —— 显著降低表格重建/公式反推幻觉。

### 5.4 配置（遵守 `backend/config.py` 约定）

新增 `_FILE_KEYS`（平铺命名，复用 OCR 同一套 provider/base_url/api_key，
不 import `ocrmypdf_unlimited` —— 引擎无关边界）：

```
export_llm            # bool，默认 false
export_llm_model      # 缺省回落到 model
export_llm_threshold  # 低置信触发阈值，默认 0.85
export_llm_batch      # 每请求块数，默认 8
export_llm_images     # bool，默认 false（P3）
export_llm_timeout_s  # 默认 120
```

env 别名进 `_ENV_ALIASES`：`OCR_EXPORT_LLM`、`OCR_EXPORT_LLM_MODEL`、…
（`resolve()` 统一读，代码里不许碰 `os.environ`）。

### 5.5 缓存 / 并发 / 安全网

- **缓存** `work/<job>/export_llm_cache.json`，键 =
  `sha256(model + prompt_v + kind + source_text)`。同 job 反复导出零成本。
- **并发** 导出是同步请求：`min(4, export_llm_batch)` 线程 + 总超时
  `export_llm_timeout_s`；超时即整体回退 P0 结果。
- **Guard**（逐块，任一失败 → 该块回退）：
  - JSON 解析失败 → 重试 1 次 → 回退；
  - 长度比 `len(out)/len(in)` ∉ [0.5, 3.0] → 回退（表格/公式放宽到 5）；
  - 数字/字母 token 集合差异 > 10% → 回退（防篡改事实）；
  - 表格：输出行数 = 0 且输入行数 > 0 → 回退；
  - 顶层 try/except：LLM 全挂 → 返回 P0 结果，导出照常 200。

### 5.6 API / CLI 面

```
GET /api/export/{job}.md?llm_blocks=1        # 块修整（独立步骤）
GET /api/export/{job}.md?llm_outline=1       # 章节精修（独立步骤，含目录）
GET /api/export/{job}.md?llm=1               # legacy master：两者
GET /api/export/stream/{job}.{ext}?...       # SSE：progress 事件 + done 携带全文
python -m backend.cli in.pdf -o out.md --export markdown \
    [--export-llm-blocks | --export-llm-outline | --export-llm]
```

配置：`export_llm_blocks` / `export_llm_outline`（独立键），`export_llm` 为
legacy master（缺省两者）。WebUI：导出不是单独按钮——Markdown/LaTeX 链接 +
「导出选项」（⚙ 下拉：LLM 修复块 / LLM 修正章节两个勾选）；勾选后点击格式
链接走 SSE 流并显示独立进度条，未勾选则是即时直下。

---

## 6. 成本量级（基于 client.py 实测注释：密集页输出 ≈1.5–2.5k tok）

| 文档 | A 块级（15% 块触发） | B 页级 | C 整文档 |
|------|--------------------|--------|---------|
| 10 页 | ~1–2 次调用，≈5–10k tok | 10 次，≈20–50k tok | 1 次，≈30–100k tok |
| 100 页 | ~10–20 次，≈50–100k tok | 100 次，≈200–500k tok | 需分段，≈300k–1M tok |

块级 + 缓存后，日常「导出 → 微调 → 再导出」循环几乎零增量成本。

---

## 7. 测试与验收

- **P0**：纯函数单测（连字符/列表/ragged/跨页），无需网络，进 `tests/`。
- **P1**：注入 fake client（`export_llm.preprocess(pages, cfg, client=fake)`），
  钉住 guard 全部分支与回退路径；用 tesseract sidecar（无 raw）跑回退 e2e。
- **质量验收**：同 job `?llm=0/1` 双导出 diff 人工对照；进阶按论文思路用下游
  QA/RAG 指标评估（对技术文档抽样 5–10 页即可）。
- **回归红线**：`llm` 字段必须不影响嵌入文本层 —— 加一条测试钉住
  `page_store.blocks_to_hocr` 忽略未知字段。

---

## 8. 与 FEATURE_IDEAS 的关系

- **#3 LLM 后处理校对**（嵌入期，OCR 后修字进文本层）：本设计是它的**导出期切片**，
  共用同一套配置与 client 模式，但 seam 不同（不动 sidecar）。二者互补，可共享
  guard/缓存代码。
- **#5 表格结构化 / 公式 LaTeX**：块级修整是其低成本实现路径之一；线检测原型
  仍是表格 bbox 精度问题的正解，不冲突。

---

## 9. 分阶段落地

| 阶段 | 内容 | 状态 |
|------|------|------|
| P1 | `backend/export_llm.py` 的 P0 确定性部分 + `block_source` 三层扩展 + 单测 | ✅ 已实现 |
| P2 | 块级 LLM：client（httpx + retry，复用 `resolve()`）、guard、缓存、`?llm=1`、`--export-llm`、config 键 | ✅ 已实现（v1 文本模式） |
| P3 | 图像取证（PyMuPDF 裁块）、整文档模式（实验）、图片实体导出（见 §11） | 未开始（WebUI 导出按钮 + SSE 进度条 ✅ 已提前落地） |

## 10. 开放问题

1. 长文档导出走同步请求还是复用 SSE job？（>50 页建议后台 job + 进度）
2. `llm` 字段要不要持久化进 sidecar（跨会话复用缓存）？—— 涉及编辑器语义，倾向不进
   （当前实现：只进导出副本，缓存落 `work/<job>/export_llm_cache.json`）。
3. 整文档模式若只服务于标题层级，可否退化为「只发各页首块 + 目录页」的廉价特例？
   —— **已全部落地**：
   * **确定性版本（P0.5）**：`assign_heading_levels` 用文档级信号定级——
     扩展编号（`第一章`/`一、`/`（一）`/`Appendix A`/Roman）优先，无编号标题按
     字号分带（bbox 行高 vs 正文行高中位数，~15% 聚类），首个标题为文档标题。
     builder 经 `block_heading_level` 读 `llm_level`。
   * **LLM 大纲精修（P1b，`?llm=1` 时默认）**：`_refine_outline` 在块修整后跑——
     发全部标题（序号|当前层级|文本）+ **检测到的目录**（`detect_toc_entries`
     从前几页解析点线引导符行，编号定深度、无编号按缩进；必须在 reflow 前检测，
     因为 reflow 会 join 点线行）作为语义 ground truth，要回
     `{"headings":[{"n","level"}]}`；逐项 guard：level ∈ 1..5、首个 ∈ {1,2}、
     层级不得一次加深 >1，不合格项回退确定性层级。语义错乱（目录说第 2 章在第
     3 章之前、无编号但语义上是章级）由此修正。
4. 表格图像取证的最小分辨率（页图 300dpi 裁块 vs 重渲染 150dpi 裁块）需实测。

## 11. 位置信息与「重现排版」在导出中的真实角色

实施中验证的两个定位结论（回答「bbox 到底有什么用」「LaTeX 连排版都做不到有什么用」）：

**bbox 在当前 markdown/LaTeX 导出里零使用。** `backend/export.py` 的 builder 只读
`kind / raw / text / caption / blocks 序`——导出的「位置性」信息是块数组序（引擎在
OCR 期定好的阅读序），不是坐标。bbox 的消费者在别处：finalize 文本层、WebUI overlay、
验证报告。bbox 进入导出管线的唯一不可替代用途是**图片实体导出**（按 bbox 裁页图 →
`![](figures/p3-1.png)` / `\includegraphics`，从注释占位符变实体）与多栏阅读序重排
（均只读坐标、不写回，列为 P3）。

**LaTeX 导出的价值不在排版保真。** 排版保真是嵌字 PDF（主产出）的本职——原版页面
像素级保留。LaTeX 导出补的是另一条轴：内容可编辑 / 数学可编译 / 可 diff。
「重现排版」只有绝对定位（`textpos` 每块一个 `\put`）一条路，是反模式：源码不可读、
编辑失效、且冗余（要视觉复刻，原版 PDF 就在那里；业界 olmOCR/MinerU/Mathpix 输出的
都是内容结构，无一复刻版面）。值得追求的是**语义结构保真**（标题层级、表格、公式、
图片），这正是 P0/P1 修整的对象。
