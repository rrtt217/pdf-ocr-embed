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
GET /api/export/{job}.md?llm=1        # query 参数，覆盖配置开关
python -m backend.cli in.pdf -o out.md --export markdown --export-llm
```

不改 `/api/health`、不动 WebUI（P2 再加导出面板开关）。

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

| 阶段 | 内容 | 依赖 |
|------|------|------|
| P1 | `backend/export_llm.py` 的 P0 确定性部分 + `block_source` 三行扩展 + 单测 | 无（离线） |
| P2 | 块级 LLM：client（httpx + retry，复用 `resolve()`）、guard、缓存、`?llm=1`、`--export-llm`、config 键 | API key |
| P3 | 图像取证（PyMuPDF 裁块）、整文档模式（实验）、WebUI 开关 | P2 |

## 10. 开放问题

1. 长文档导出走同步请求还是复用 SSE job？（>50 页建议后台 job + 进度）
2. `llm` 字段要不要持久化进 sidecar（跨会话复用缓存）？—— 涉及编辑器语义，倾向不进。
3. 整文档模式若只服务于标题层级，可否退化为「只发各页首块 + 目录页」的廉价特例？
4. 表格图像取证的最小分辨率（页图 300dpi 裁块 vs 重渲染 150dpi 裁块）需实测。
