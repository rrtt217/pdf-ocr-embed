/* i18n for PDF OCR Embed — English ("en") + 中文 ("zh"). No build step.
 *
 * Usage:
 *   I18N.t("key")                    -> translated string (falls back to en, then the key)
 *   I18N.t("key", {name: "x"})       -> with {name} interpolation
 *   I18N.setLocale("zh")             -> switch locale (persists + re-applies DOM)
 *   I18N.applyDocument()             -> translate every [data-i18n*] node in the DOM
 *
 * Static markup declares data-i18n / data-i18n-html / data-i18n-title /
 * data-i18n-placeholder / data-i18n-alt attributes; dynamic strings in app.js
 * call I18N.t() directly. A CustomEvent "i18n:changed" fires on locale switch
 * so the app can re-render dynamic parts.
 */
"use strict";

const I18N_DICT = {
  en: {
    // --- connection / status pill ---
    "status.idle": "idle",
    "status.online": "online",
    "status.offline": "offline",
    "status.running": "running",
    "status.uploading": "uploading…",
    "status.preview": "previewing…",
    "status.dirty": "unsaved changes",
    "status.embedded": "embedded",
    "status.error": "error",

    // --- job status pills ---
    "job.uploaded": "starting",
    "job.running": "running",
    "job.retrying": "retrying",
    "job.stopped": "stopped",
    "job.done": "done",
    "job.error": "error",
    "job.embedded": "embedded",

    // --- header ---
    "header.language": "Language",
    "header.theme": "Theme",
    "theme.auto": "Auto",
    "theme.light": "Light",
    "theme.dark": "Dark",
    "header.cleanup": "Cleanup",
    "header.cleanupTitle": "Delete orphaned/old working files and outputs",
    "header.logs": "Logs",
    "header.settings": "Settings",

    // --- upload ---
    "upload.drop": "Drop a scanned PDF here",
    "upload.orClick": "or click to choose a file. Pages are rasterized, OCR'd, and made searchable.",
    "upload.engine": "OCR engine",
    "upload.engine.unlimited": "Unlimited OCR (API)",
    "upload.engine.tesseract": "Tesseract (local)",
    "upload.engine.generic": "Generic OpenAI (API)",
    "upload.lang": "Tesseract language",
    "upload.concurrency": "Parallel OCR pages",
    "upload.hint.tesseract": "Local OCR — no API key needed. Configure Tesseract language (e.g. chi_sim, eng, chi_sim+eng).",
    "upload.hint.unlimited": "Higher = faster on many pages, but more concurrent API calls.",
    "upload.hint.generic": "Generic OpenAI vision model — set API key / base URL / model in Settings.",
    "upload.notPdf": "Please choose a PDF file.",
    "upload.failed": "Upload failed: {msg}",
    "upload.started": "Job started — OCR is running.",

    // --- jobs ---
    "jobs.title": "Jobs",
    "jobs.hint": "All tasks on the server — close this page and come back anytime, they survive.",
    "job.stop": "Stop",
    "job.stopping": "Stopping…",
    "job.retry": "Retry",
    "job.retryRemaining": "Retry remaining",
    "job.downloadPartial": "Download partial",
    "job.clear": "Clear",
    "job.clearConfirm": "Delete job \"{name}\" entirely?\n\nThis removes its OCR results, working files and any embedded PDF.",
    "job.editPages": "Edit pages",
    "job.embeddedPdf": "⬇ Embedded PDF",
    "job.stopFailed": "Stop failed: {msg}",
    "job.retryFailed": "Retry failed: {msg}",
    "job.partialFailed": "Partial failed: {msg}",
    "job.noPages": "No completed pages",

    // --- workspace / editor ---
    "workspace.editing": "Editing: {name}",
    "workspace.prev": "Previous page (←)",
    "workspace.next": "Next page (→)",
    "workspace.blockTitle": "Editable text blocks",
    "workspace.previewTitle": "Page preview",
    "workspace.embedFont": "Embed font",
    "workspace.embedFontTitle": "System font used for the embedded text layer and preview",
    "workspace.autoFont": "Auto (default)",
    "workspace.previewOverlay": "Preview overlay",
    "workspace.previewOverlayTitle": "Draw the placed text visibly so you can judge font size",
    "workspace.embed": "Embed invisible text",
    "workspace.embedTitle": "Bake the text layer into a new PDF (Ctrl+Enter)",
    "workspace.downloadEmbedded": "Download embedded PDF",
    "workspace.downloadDataset": "Download dataset (JSON)",
    "workspace.downloadDatasetTitle": "Download the per-block font-scale dataset (JSON)",
    "workspace.hint": "Use each block's <em>font size</em> slider (below) to fine-tune — bigger/smaller — then <strong>Preview overlay</strong> to see the placed text on the page, and <strong>Embed</strong> to bake your chosen sizes in.",
    "editor.noBlocks": "No text blocks on this page.",
    "editor.placeholder": "Text…",
    "editor.removeBlock": "Remove block",
    "editor.fontSize": "font size",
    "editor.auto": "auto",
    "editor.autoTitle": "Reset to auto (1.0)",
    "editor.noPagesYet": "No pages OCR'd yet — they appear here as each page finishes.",
    "editor.confLowOnly": "Low confidence only",
    "editor.confThreshold": "Threshold %",
    "editor.confCount": "{n} low-confidence block(s) below {p}%",
    "editor.confNone": "No low-confidence blocks (below {p}%)",
    "editor.confNoData": "This engine reports no confidence (Tesseract does)",
    "editor.confNoLowOnPage": "No low-confidence blocks on this page (below {p}%)",
    "editor.confPageBadge": "{n} low-confidence block(s)",
    "editor.confNa": "—",
    "workspace.outputOptions": "Output options",
    "workspace.imgMode": "Image mode",
    "workspace.imgMode.none": "Keep original",
    "workspace.imgMode.jpeg": "Recompress JPEG",
    "workspace.imgMode.grayjpeg": "Grayscale JPEG",
    "workspace.imgQuality": "JPEG quality",
    "workspace.imgDownscale": "Downscale",
    "workspace.imgDownscale.none": "None",
    "workspace.imgDownscale.half": "Half",
    "workspace.imgDownscale.quarter": "Quarter",
    "workspace.linearize": "Fast web view (linearize)",
    "embed.optStats": "Images: {n} replaced, saved {bytes}.",
    "embed.linearized": "Linearized for fast web view.",
    "embed.linearUnavailable": "Linearization unavailable in this build (saved normally).",

    // --- embed ---
    "embed.busy": "Embedding…",
    "embed.download": "Download {name}",
    "embed.done": "Embedded. Text is now selectable/searchable.",
    "embed.failed": "Embed failed: {msg}",
    "embed.previewFailed": "Preview failed: {msg}",
    "toast.embedDone": "Embedded PDF ready.",
    "fontInfo.line": "{derived}pt → {fs}pt ({lines} ln)",

    // --- settings ---
    "settings.title": "OCR Settings",
    "settings.hint": "Settings are read from the local <code>backend/ocr_config.toml</code>; saving writes back to that file. A masked key means one is already configured.",
    "settings.provider": "Provider preset",
    "settings.provider.ustc": "USTC (OpenAI-compatible)",
    "settings.provider.openai": "OpenAI",
    "settings.provider.custom": "Custom",
    "settings.baseurl": "Base URL",
    "settings.model": "Model",
    "settings.apikey": "API Key",
    "settings.save": "Save",
    "settings.cancel": "Cancel",
    "settings.saved": "Saved.",
    "settings.loadFailed": "Failed to load settings: {msg}",
    "settings.saveFailed": "Save failed: {msg}",

    // --- cleanup ---
    "cleanup.title": "Temporary file cleanup",
    "cleanup.intro": "Working files (<code>work/</code>), embedded PDFs and overlays (<code>output/</code>) are only referenced while the server is running. <strong>Unreferenced</strong> files older than the age limit are safe to delete; anything a live job still uses is always kept.",
    "cleanup.interval": "Automatic background cleanup every",
    "cleanup.hoursUnit": "hour(s)",
    "cleanup.loading": "Loading…",
    "cleanup.olderThan": "Delete unreferenced files older than",
    "cleanup.preview": "Preview",
    "cleanup.cleanNow": "Clean now",
    "cleanup.cancel": "Cancel",
    "cleanup.total": "{n} file(s) ready to clean right now (older than {age} h), {bytes}",
    "cleanup.areaReady": "ready",
    "cleanup.areaUnref": "unreferenced total",
    "cleanup.areaInUse": "in use",
    "cleanup.area.work": "work",
    "cleanup.area.output": "output",
    "cleanup.area.uploads": "uploads",
    "cleanup.none": "No unreferenced files are old enough yet — lower the limit or use Preview to see younger candidates.",
    "cleanup.loadFailed": "Failed to load cleanup info: {msg}",
    "cleanup.previewing": "Previewing…",
    "cleanup.cleaning": "Cleaning…",
    "cleanup.previewDone": "Preview: would delete {n} file(s), freeing {bytes}. Kept: {inUse} in use / {fresh} too fresh.",
    "cleanup.done": "Deleted {n} file(s), freed {bytes}. Kept {inUse} in use.",
    "cleanup.failed": "Cleanup failed: {msg}",

    // --- OCR result cache ---
    "cache.title": "OCR result cache",
    "cache.intro": "Pages already recognized with the same document + engine + settings are stored on disk so re-uploads finish instantly without re-rendering or re-billing the engine. Clearing frees the disk; the next run will redo recognition from scratch.",
    "cache.entries": "{n} cached entries, {bytes}",
    "cache.hitsMisses": "{hits} hit(s) / {misses} miss(es)",
    "cache.ttlHours": "entries expire after {h} hours",
    "cache.disabled": "disabled in backend settings (ocr_cache_enabled)",
    "cache.clear": "Clear OCR cache",
    "cache.clearing": "Clearing cache…",
    "cache.cleared": "Cleared {n} entries, freed {bytes}.",
    "cache.failed": "Cache clear failed: {msg}",
    "cache.loadFailed": "Failed to load cache info: {msg}",

    // --- logs ---
    "logs.title": "Debug logs",
    "logs.refresh": "Refresh",
    "logs.auto": "Auto",

    // --- dataset download ---
    "dataset.def": "font_scale multiplies the auto font size",
  },

  zh: {
    // --- connection / status pill ---
    "status.idle": "空闲",
    "status.online": "在线",
    "status.offline": "离线",
    "status.running": "处理中",
    "status.uploading": "上传中…",
    "status.preview": "预览中…",
    "status.dirty": "有未保存修改",
    "status.embedded": "已嵌入",
    "status.error": "出错",

    // --- job status pills ---
    "job.uploaded": "启动中",
    "job.running": "处理中",
    "job.retrying": "重试中",
    "job.stopped": "已停止",
    "job.done": "完成",
    "job.error": "出错",
    "job.embedded": "已嵌入",

    // --- header ---
    "header.language": "语言",
    "header.theme": "主题",
    "theme.auto": "自适应",
    "theme.light": "浅色",
    "theme.dark": "深色",
    "header.cleanup": "清理",
    "header.cleanupTitle": "删除孤儿/过期的临时工作文件与输出",
    "header.logs": "日志",
    "header.settings": "设置",

    // --- upload ---
    "upload.drop": "将扫描版 PDF 拖到这里",
    "upload.orClick": "或点击选择文件。每页会被栅格化、OCR 识别并生成可搜索的文字层。",
    "upload.engine": "OCR 引擎",
    "upload.engine.unlimited": "Unlimited OCR（API）",
    "upload.engine.tesseract": "Tesseract（本地）",
    "upload.engine.generic": "通用 OpenAI（API）",
    "upload.lang": "Tesseract 语言",
    "upload.concurrency": "并行 OCR 页数",
    "upload.hint.tesseract": "本地 OCR——无需 API 密钥。可配置 Tesseract 语言（如 chi_sim、eng、chi_sim+eng）。",
    "upload.hint.unlimited": "页数越多处理越快，但并发 API 调用也越多。",
    "upload.hint.generic": "通用 OpenAI 视觉模型——请在设置中配置 API 密钥 / 基础 URL / 模型。",
    "upload.notPdf": "请选择一个 PDF 文件。",
    "upload.failed": "上传失败：{msg}",
    "upload.started": "任务已开始，OCR 处理中。",

    // --- jobs ---
    "jobs.title": "任务",
    "jobs.hint": "服务器上的全部任务——关闭页面后随时回来，任务依然保留。",
    "job.stop": "停止",
    "job.stopping": "停止中…",
    "job.retry": "重试",
    "job.retryRemaining": "重试剩余页",
    "job.downloadPartial": "下载部分结果",
    "job.clear": "清除",
    "job.clearConfirm": "彻底删除任务“{name}”？\n\n这将移除其 OCR 结果、工作文件以及任何嵌入后的 PDF。",
    "job.editPages": "编辑页面",
    "job.embeddedPdf": "⬇ 嵌入后的 PDF",
    "job.stopFailed": "停止失败：{msg}",
    "job.retryFailed": "重试失败：{msg}",
    "job.partialFailed": "获取部分结果失败：{msg}",
    "job.noPages": "没有已完成的页面",

    // --- workspace / editor ---
    "workspace.editing": "正在编辑：{name}",
    "workspace.prev": "上一页（←）",
    "workspace.next": "下一页（→）",
    "workspace.blockTitle": "可编辑文本块",
    "workspace.previewTitle": "页面预览",
    "workspace.embedFont": "嵌入字体",
    "workspace.embedFontTitle": "嵌入文字层与预览所用的系统字体",
    "workspace.autoFont": "自动（默认）",
    "workspace.previewOverlay": "预览叠加层",
    "workspace.previewOverlayTitle": "将排版文字以可见方式绘制在页面上，便于判断字号",
    "workspace.embed": "嵌入隐形文本",
    "workspace.embedTitle": "将文字层写入新的 PDF（Ctrl+Enter）",
    "workspace.downloadEmbedded": "下载嵌入后的 PDF",
    "workspace.downloadDataset": "下载数据集（JSON）",
    "workspace.downloadDatasetTitle": "下载按块统计的字号缩放数据集（JSON）",
    "workspace.hint": "用每个文本块的<em>字号</em>滑块（下方）微调——调大/调小——然后点<strong>预览叠加层</strong>查看页面上的排版效果，最后点<strong>嵌入</strong>固化所选字号。",
    "editor.noBlocks": "此页没有文本块。",
    "editor.placeholder": "文本…",
    "editor.removeBlock": "删除文本块",
    "editor.fontSize": "字号",
    "editor.auto": "自动",
    "editor.autoTitle": "重置为自动（1.0）",
    "editor.noPagesYet": "还没有完成 OCR 的页面——每完成一页就会出现在这里。",
    "editor.confLowOnly": "只看低置信度",
    "editor.confThreshold": "阈值 %",
    "editor.confCount": "{n} 个低置信块（低于 {p}%）",
    "editor.confNone": "没有低置信块（低于 {p}%）",
    "editor.confNoData": "当前引擎未提供置信度（仅 Tesseract 提供）",
    "editor.confNoLowOnPage": "此页没有低置信块（低于 {p}%）",
    "editor.confPageBadge": "{n} 个低置信块",
    "editor.confNa": "—",
    "workspace.outputOptions": "输出选项",
    "workspace.imgMode": "图片模式",
    "workspace.imgMode.none": "保持原样",
    "workspace.imgMode.jpeg": "重压 JPEG",
    "workspace.imgMode.grayjpeg": "灰度 JPEG",
    "workspace.imgQuality": "JPEG 质量",
    "workspace.imgDownscale": "降采样",
    "workspace.imgDownscale.none": "无",
    "workspace.imgDownscale.half": "1/2",
    "workspace.imgDownscale.quarter": "1/4",
    "workspace.linearize": "快速网页查看（线性化）",
    "embed.optStats": "图片：重压 {n} 张，省 {bytes}。",
    "embed.linearized": "已线性化，适合网页快速查看。",
    "embed.linearUnavailable": "当前构建不支持线性化（已按普通方式保存）。",

    // --- embed ---
    "embed.busy": "嵌入中…",
    "embed.download": "下载 {name}",
    "embed.done": "嵌入完成。文字现在可选中 / 可搜索。",
    "embed.failed": "嵌入失败：{msg}",
    "embed.previewFailed": "预览失败：{msg}",
    "toast.embedDone": "嵌入后的 PDF 已就绪。",
    "fontInfo.line": "{derived}pt → {fs}pt（{lines} 行）",

    // --- settings ---
    "settings.title": "OCR 设置",
    "settings.hint": "设置读取自本地 <code>backend/ocr_config.toml</code>，保存时写回该文件。若显示掩码密钥，说明已配置。",
    "settings.provider": "服务商预设",
    "settings.provider.ustc": "USTC（兼容 OpenAI）",
    "settings.provider.openai": "OpenAI",
    "settings.provider.custom": "自定义",
    "settings.baseurl": "基础 URL",
    "settings.model": "模型",
    "settings.apikey": "API 密钥",
    "settings.save": "保存",
    "settings.cancel": "取消",
    "settings.saved": "已保存。",
    "settings.loadFailed": "加载设置失败：{msg}",
    "settings.saveFailed": "保存失败：{msg}",

    // --- cleanup ---
    "cleanup.title": "临时文件清理",
    "cleanup.intro": "工作文件（<code>work/</code>）、嵌入后的 PDF 与叠加图（<code>output/</code>）仅在服务器运行期间被引用。<strong>未被引用</strong>且超过时限的文件可以安全删除；正在被任务使用的文件始终保留。",
    "cleanup.interval": "后台自动清理间隔：",
    "cleanup.hoursUnit": "小时",
    "cleanup.loading": "加载中…",
    "cleanup.olderThan": "删除早于以下时限的未引用文件",
    "cleanup.preview": "预览",
    "cleanup.cleanNow": "立即清理",
    "cleanup.cancel": "取消",
    "cleanup.total": "当前有 {n} 个文件可清理（早于 {age} 小时），共 {bytes}",
    "cleanup.areaReady": "可清理",
    "cleanup.areaUnref": "未引用总计",
    "cleanup.areaInUse": "使用中",
    "cleanup.area.work": "work",
    "cleanup.area.output": "output",
    "cleanup.area.uploads": "uploads",
    "cleanup.none": "还没有足够旧的未引用文件——可降低时限，或用预览查看更年轻的文件。",
    "cleanup.loadFailed": "加载清理信息失败：{msg}",
    "cleanup.previewing": "预览中…",
    "cleanup.cleaning": "清理中…",
    "cleanup.previewDone": "预览：将删除 {n} 个文件，释放 {bytes}。保留：{inUse} 个使用中 / {fresh} 个太新。",
    "cleanup.done": "已删除 {n} 个文件，释放 {bytes}。保留 {inUse} 个使用中。",
    "cleanup.failed": "清理失败：{msg}",

    // --- OCR result cache ---
    "cache.title": "OCR 结果缓存",
    "cache.intro": "相同文档 + 相同引擎与参数的已识别页会存到磁盘，再次上传同一 PDF 时直接命中、无需重新渲染或再次扣费。清空只释放磁盘空间；之后重跑会从头识别（API 引擎会重新计费）。",
    "cache.entries": "{n} 个缓存条目，共 {bytes}",
    "cache.hitsMisses": "命中 {hits} / 未命中 {misses}",
    "cache.ttlHours": "条目 {h} 小时后过期",
    "cache.disabled": "已在后端配置中关闭（ocr_cache_enabled）",
    "cache.clear": "清空 OCR 缓存",
    "cache.clearing": "正在清空…",
    "cache.cleared": "已清空 {n} 个条目，释放 {bytes}。",
    "cache.failed": "清空缓存失败：{msg}",
    "cache.loadFailed": "加载缓存信息失败：{msg}",

    // --- logs ---
    "logs.title": "调试日志",
    "logs.refresh": "刷新",
    "logs.auto": "自动",

    // --- dataset download ---
    "dataset.def": "font_scale 为自动字号的倍率",
  },
};

const I18N = {
  locale: "en",

  t(key, params) {
    const dict = I18N_DICT[this.locale] || I18N_DICT.en;
    let s = dict[key] != null ? dict[key] : I18N_DICT.en[key] != null ? I18N_DICT.en[key] : key;
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        s = s.split("{" + k + "}").join(String(v));
      }
    }
    return s;
  },

  setLocale(locale, persist = true) {
    this.locale = I18N_DICT[locale] ? locale : "en";
    if (persist) {
      try { localStorage.setItem("pdfocr.ui.locale", this.locale); } catch { /* ignore */ }
    }
    document.documentElement.lang = this.locale;
    this.applyDocument();
    document.dispatchEvent(new CustomEvent("i18n:changed", { detail: { locale: this.locale } }));
  },

  /* Translate every declarative [data-i18n*] node in the document. */
  applyDocument() {
    const sel = "[data-i18n], [data-i18n-html], [data-i18n-title], [data-i18n-placeholder], [data-i18n-alt]";
    document.querySelectorAll(sel).forEach((node) => {
      if (node.hasAttribute("data-i18n")) node.textContent = this.t(node.getAttribute("data-i18n"));
      if (node.hasAttribute("data-i18n-html")) node.innerHTML = this.t(node.getAttribute("data-i18n-html"));
      if (node.hasAttribute("data-i18n-title")) node.title = this.t(node.getAttribute("data-i18n-title"));
      if (node.hasAttribute("data-i18n-placeholder")) node.placeholder = this.t(node.getAttribute("data-i18n-placeholder"));
      if (node.hasAttribute("data-i18n-alt")) node.setAttribute("alt", this.t(node.getAttribute("data-i18n-alt")));
    });
  },
};