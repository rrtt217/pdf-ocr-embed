/* PDF OCR Embed single-page WebUI — multi-job.

   The backend is the single source of truth for every OCR task: it holds them
   all in memory, so this page simply asks "what jobs are there?" on load and
   subscribes to live progress (SSE) per running job. Jobs survive closing the
   tab; several may run in parallel and each is managed from its own card.

   View preferences (theme, locale, engine, zoom…) are persisted in
   localStorage — job data itself is never stored client-side.
*/
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const t = (key, params) => I18N.t(key, params);

const RUNNING_STATUSES = new Set(["uploaded", "running", "retrying"]);
// status -> i18n key (rendered through t())
const STATUS_LABEL = {
  uploaded: "job.uploaded",
  running: "job.running",
  retrying: "job.retrying",
  stopped: "job.stopped",
  done: "job.done",
  error: "job.error",
  embedded: "job.embedded",
};

/* Persisted UI preferences (theme/locale/engine/zoom…). localStorage only —
   job data itself stays server-side, these are purely view preferences. */
const PREFS = {
  theme: "pdfocr.ui.theme",
  locale: "pdfocr.ui.locale",
  adapter: "pdfocr.ui.adapter",
  tessLang: "pdfocr.ui.tessLang",
  concurrency: "pdfocr.ui.concurrency",
  zoom: "pdfocr.ui.zoom",
  confFilter: "pdfocr.ui.confFilter",
  confThreshold: "pdfocr.ui.confThreshold",
  optimize: "pdfocr.ui.optimize",
  outputType: "pdfocr.ui.outputType",
};

function getPref(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch { return fallback; }
}

function setPref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* ignore */ }
}

const state = {
  jobs: [],     // job summaries: {id, filename, status, current, total, error, has_embedded, created, busy}
  sel: null,    // editor session for the selected job: {jobId, pages, pageIndex, embedded}
  zipSel: new Set(),  // job ids ticked for the "Download ZIP" batch action
  pendingFiles: [],   // staged uploads: {file, engine, lang, concurrency, pageStart, pageEnd}
  zoom: 100,
  es: {},       // jobId -> EventSource
  logTimer: null,
  confFilter: false,   // show only low-confidence blocks in the editor
  confThreshold: 60,   // 1..100 — blocks below are flagged low-confidence
  exportLlm: { blocks: false, outline: false },  // export LLM post-processing options
  exportImages: "none",  // markdown image embedding: none | zip | base64
  exportSplit: false,    // markdown: one file per chapter, packed as a ZIP
};

/* ---------- helpers ---------- */
/* ---------- confidence review (#1) ---------- */
function confPct(block) {
  const c = block && block.conf;
  if (typeof c !== "number" || !isFinite(c)) return null;
  return c > 1 ? c : c * 100;   // engines report 0..100 or 0..1
}

function confClass(pct) {
  if (pct === null) return "conf-na";
  return pct >= 85 ? "conf-high" : pct >= 60 ? "conf-med" : "conf-low";
}

function isLowConf(block) {
  const p = confPct(block);
  return p !== null && p < state.confThreshold;
}

function pageLowConfCount(page) {
  return (page.blocks || []).filter(isLowConf).length;
}

function jobHasConfData() {
  const sel = state.sel;
  return !!sel && sel.pages.some((pg) => (pg.blocks || [])
    .some((b) => confPct(b) !== null));
}
function setStatus(code, cls) {
  const elx = $("#conn-status");
  elx.textContent = t("status." + code);
  elx.className = "pill" + (cls ? " " + cls : "");
}

/* ---------- theme (light / dark / auto) ---------- */
const MEDIA_DARK = window.matchMedia("(prefers-color-scheme: dark)");

function effectiveTheme(pref) {
  if (pref === "light" || pref === "dark") return pref;
  return MEDIA_DARK.matches ? "dark" : "light";
}

function applyTheme(pref) {
  const eff = effectiveTheme(pref);
  document.documentElement.dataset.theme = eff;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", eff === "dark" ? "#12161c" : "#f5f6f8");
}

/* ---------- toasts ---------- */
function toast(msg, type) {
  let box = $("#toast-box");
  if (!box) {
    box = el("div", "toast-box");
    box.id = "toast-box";
    document.body.appendChild(box);
  }
  const node = el("div", "toast" + (type ? " " + type : ""), msg);
  box.appendChild(node);
  setTimeout(() => {
    node.classList.add("out");
    setTimeout(() => node.remove(), 350);
  }, 4000);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    const err = new Error(body || ("HTTP " + res.status));
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function jobById(id) {
  return state.jobs.find((j) => j.id === id);
}

function anyRunning() {
  return state.jobs.some((j) => RUNNING_STATUSES.has(j.status));
}

function setGlobalStatus() {
  if (state.jobs.length === 0) setStatus("idle", "");
  else if (anyRunning()) setStatus("running", "running");
  else setStatus("idle", "done");
}

/* ---------- upload (one of possibly many parallel jobs) ---------- */
function updateAdapterUI() {
  const adapter = $("#adapter").value;
  const langRow = $("#tess-lang-row");
  const hint = $("#adapter-hint");
  if (adapter === "tesseract") {
    langRow.classList.remove("hidden");
    hint.textContent = t("upload.hint.tesseract");
  } else {
    langRow.classList.add("hidden");
    hint.textContent = adapter === "unlimited"
      ? t("upload.hint.unlimited")
      : t("upload.hint.tesseract");
  }
}

function currentEngineCfg() {
  // The engine select is shared by upload / retry; the language field applies
  // to Tesseract (the unlimited engine ignores it).
  const cfg = { ocr_engine: $("#adapter").value };
  if (cfg.ocr_engine === "tesseract") {
    const lang = $("#tess-lang").value.trim();
    if (lang) cfg.lang = lang;
  }
  return cfg;
}

async function uploadOne(file, cfg) {
  const fd = new FormData();
  fd.append("files", file);            // multi-file field (a single file works too)
  fd.append("ocr_engine", cfg.ocr_engine);
  if (cfg.lang) fd.append("lang", cfg.lang);
  fd.append("concurrency", String(Math.max(1, Math.min(32,
    parseInt(cfg.concurrency || "1", 10) || 1))));
  if (cfg.page_start) fd.append("page_start", String(cfg.page_start));
  if (cfg.page_end) fd.append("page_end", String(cfg.page_end));
  return api("/api/ocr/upload", { method: "POST", body: fd });
}

/* Drop/select: PDFs are NOT started right away.  Design change — the task must
   wait until the user picks parameters for each file (engine / language /
   concurrency / page range) and clicks "Start OCR"; only then are the jobs
   created on the server.  Files are staged in state.pendingFiles and shown in
   the inline #pending-panel inside the upload card. */
async function handleFiles(fileList) {
  const msg = $("#upload-msg");
  if (msg) msg.textContent = "";
  const files = Array.from(fileList || []).filter(
    (f) => f && f.name && f.name.toLowerCase().endsWith(".pdf"));
  if (files.length === 0) {
    if (fileList && fileList.length) {
      setStatus("error", "error");
      if (msg) msg.textContent = t("upload.notPdf");
    }
    return;
  }
  // Defaults for the new file(s) come from the shared upload/retry options
  // (which remember the last choices through prefs).
  const d = pendingDefaults();
  state.pendingFiles = (state.pendingFiles || []).concat(
    files.map((f) => ({
      file: f,
      engine: d.engine,
      lang: d.lang,
      concurrency: d.concurrency,
      pageStart: "",
      pageEnd: "",
    })));
  renderPendingPanel();
  toast(t("upload.pending", { n: state.pendingFiles.length }), "info");
}

function pendingDefaults() {
  return {
    engine: $("#adapter") ? $("#adapter").value : "unlimited",
    lang: $("#tess-lang") ? ($("#tess-lang").value || "").trim() : "",
    concurrency: String(Math.max(1, Math.min(32,
      parseInt($("#concurrency") ? $("#concurrency").value : "1", 10) || 1))),
  };
}

function renderPendingPanel() {
  const panel = $("#pending-panel");
  const box = $("#pending-files");
  const list = state.pendingFiles || [];
  panel.classList.toggle("hidden", list.length === 0);
  const title = $("#pending-title");
  if (title) title.textContent = t("upload.pending", { n: list.length });
  if (box) box.innerHTML = "";
  list.forEach((p, i) => box.appendChild(buildPendingRow(p, i)));
  validatePendingPanel();
}

/* One per-file parameter row.  Every control writes back into its staged
   entry (state.pendingFiles[i]) so a locale-triggered re-render or the
   discard path never loses what the user typed. */
function buildPendingRow(p, i) {
  const row = el("div", "pending-file");
  row.dataset.i = String(i);
  const head = el("div", "pending-file-head");
  head.appendChild(el("span", "job-filename", p.file.name));
  head.appendChild(el("span", "hint", fmtBytes(p.file.size)));
  row.appendChild(head);

  const opts = el("div", "pending-file-opts");

  const eng = el("label", "inline-label");
  eng.appendChild(el("span", null, t("upload.engine")));
  const engSel = el("select", "sf-engine");
  [["unlimited", t("upload.engine.unlimited")],
   ["tesseract", t("upload.engine.tesseract")],
   ["none", t("upload.engine.none")]].forEach(([v, label]) => {
    const o = el("option", null, label);
    o.value = v;
    engSel.appendChild(o);
  });
  engSel.value = p.engine;
  engSel.onchange = () => {
    p.engine = engSel.value;
    const langRow = row.querySelector(".sf-lang-row");
    if (langRow) langRow.classList.toggle("hidden", p.engine !== "tesseract");
  };
  eng.appendChild(engSel);
  opts.appendChild(eng);

  const lang = el("label",
    "inline-label sf-lang-row" + (p.engine === "tesseract" ? "" : " hidden"));
  lang.appendChild(el("span", null, t("upload.lang")));
  const langIn = el("input", "sf-lang");
  langIn.type = "text";
  langIn.placeholder = "eng";
  langIn.value = p.lang;
  langIn.oninput = () => { p.lang = langIn.value.trim(); };
  lang.appendChild(langIn);
  opts.appendChild(lang);

  const conc = el("label", "inline-label");
  conc.appendChild(el("span", null, t("upload.concurrency")));
  const concIn = el("input", "sf-conc");
  concIn.type = "number";
  concIn.min = "1";
  concIn.max = "32";
  concIn.value = p.concurrency;
  concIn.oninput = () => { p.concurrency = concIn.value.trim(); };
  conc.appendChild(concIn);
  opts.appendChild(conc);

  const ps = el("label", "inline-label");
  ps.appendChild(el("span", null, t("upload.pageStart")));
  const psIn = el("input", "sf-start");
  psIn.type = "number";
  psIn.min = "1";
  psIn.placeholder = "1";
  psIn.value = p.pageStart;
  psIn.oninput = () => { p.pageStart = psIn.value.trim(); validatePendingPanel(); };
  ps.appendChild(psIn);
  opts.appendChild(ps);

  const pe = el("label", "inline-label");
  pe.appendChild(el("span", null, t("upload.pageEnd")));
  const peIn = el("input", "sf-end");
  peIn.type = "number";
  peIn.min = "1";
  peIn.placeholder = "all";
  peIn.value = p.pageEnd;
  peIn.oninput = () => { p.pageEnd = peIn.value.trim(); validatePendingPanel(); };
  pe.appendChild(peIn);
  opts.appendChild(pe);

  row.appendChild(opts);
  return row;
}

/* Parse a staged entry's page range (1-based inclusive).  Both bounds empty
   -> {set:false}.  Open bounds stay null (the server fills them from the
   document's page count).  Invalid (start<1 / end<1 / start>end) -> invalid. */
function pendingRange(p) {
  const sRaw = (p.pageStart || "").trim();
  const eRaw = (p.pageEnd || "").trim();
  const bothEmpty = sRaw === "" && eRaw === "";
  const s = sRaw === "" ? 1 : parseInt(sRaw, 10);
  const e = eRaw === "" ? null : parseInt(eRaw, 10);
  if (bothEmpty) return { set: false, invalid: false, start: null, end: null };
  const invalid = !Number.isFinite(s) || s < 1 ||
    (e !== null && (!Number.isFinite(e) || e < 1)) || (e !== null && s > e);
  return { set: true, invalid, start: s, end: e };
}

function validatePendingPanel() {
  const btn = $("#btn-start-jobs");
  const msgEl = $("#pending-msg");
  const list = state.pendingFiles || [];
  if (!list.length) {
    if (btn) btn.disabled = true;
    if (msgEl) msgEl.textContent = "";
    return;
  }
  const invalid = list.some((p) => pendingRange(p).invalid);
  if (btn) btn.disabled = invalid;
  if (msgEl) msgEl.textContent = invalid ? t("upload.pageRangeInvalid") : "";
}

/* "Start OCR": upload every staged file with ITS OWN parameters, one request
   per file (so per-file page ranges reach the backend).  Only afterwards is
   the job list reloaded; the started files leave the panel while failures
   stay staged so the user can fix (or discard) them. */
async function startPendingJobs() {
  const list = state.pendingFiles || [];
  if (!list.length) return;
  const btn = $("#btn-start-jobs");
  if (btn) btn.disabled = true;
  const msgEl = $("#pending-msg");
  if (msgEl) msgEl.textContent = t("upload.starting");
  setStatus("uploading", "running");

  const settled = [];
  for (let i = 0; i < list.length; i++) {
    const p = list[i];
    const range = pendingRange(p);
    if (range.invalid) continue;  // validation above disabled Start anyway
    const cfg = { ocr_engine: p.engine, concurrency: p.concurrency || "1" };
    if (p.engine === "tesseract" && p.lang) cfg.lang = p.lang;
    if (range.set) {
      cfg.page_start = range.start;
      if (range.end !== null) cfg.page_end = range.end;
    }
    try {
      const res = await uploadOne(p.file, cfg);
      settled.push({ ok: true, i, jobId: res.job_id, cfg });
    } catch (e) {
      settled.push({ ok: false, i, name: p.file.name, msg: e.message || String(e) });
    }
  }
  const ok = settled.filter((r) => r.ok);
  const failed = settled.filter((r) => !r.ok);

  await loadJobs();  // authoritative job list + SSE (unique per started job)

  // Drop started files; failures stay staged for a retry (or discard).
  const failedIdx = new Set(failed.map((r) => r.i));
  state.pendingFiles = list.filter((_, i) => failedIdx.has(i));
  if (ok.length && ok[0].cfg) syncSharedFromConfig(ok[0].cfg);
  renderPendingPanel();

  if (ok.length === 1) {
    selectJob(ok[0].jobId);
    toast(t("upload.started"), "success");
  } else if (ok.length > 1) {
    toast(t("upload.multiStarted", { n: ok.length }), "success");
  }
  if (failed.length) {
    const errMsg = failed[0].msg || "";
    toast(t("upload.failedSome", { n: failed.length, msg: errMsg }), "error");
    if (msgEl) msgEl.textContent = t("upload.failedSome", { n: failed.length, msg: errMsg });
  }
  setStatus(anyRunning() ? "running" : "idle", anyRunning() ? "running" : "");
}

/* Remember the choices actually used, so the shared upload/retry options
   become the defaults for the next dropped file. */
function syncSharedFromConfig(cfg) {
  const engine = cfg.ocr_engine || "unlimited";
  const adapterEl = $("#adapter");
  if (adapterEl) {
    adapterEl.value = engine;
    setPref(PREFS.adapter, engine);
    updateAdapterUI();
  }
  if (engine === "tesseract" && cfg.lang) {
    const langEl = $("#tess-lang");
    if (langEl) {
      langEl.value = cfg.lang;
      setPref(PREFS.tessLang, cfg.lang);
    }
  }
  const concEl = $("#concurrency");
  if (concEl && cfg.concurrency) {
    concEl.value = cfg.concurrency;
    setPref(PREFS.concurrency, cfg.concurrency);
  }
}

function discardPending() {
  state.pendingFiles = [];
  renderPendingPanel();
}

/* ---------- ZIP download of selected embedded jobs ---------- */
function updateZipButton() {
  const btn = $("#btn-zip");
  if (!btn) return;
  const n = state.zipSel.size;
  btn.disabled = n === 0;
  btn.textContent = n ? t("jobs.zipSel", { n }) : t("jobs.zip");
}

function toggleZipSel(jobId, checked) {
  if (checked) state.zipSel.add(jobId);
  else state.zipSel.delete(jobId);
  updateZipButton();
}

function downloadZip() {
  const n = state.zipSel.size;
  if (!n) return;
  const a = el("a");
  a.href = "/api/ocr/zip?jobs=" + encodeURIComponent(Array.from(state.zipSel).join(","));
  a.download = "ocr_results.zip";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* ---------- jobs: list + per-job SSE ---------- */
async function loadJobs() {
  try {
    const data = await api("/api/jobs");
    state.jobs = (data.jobs || []).map((j) => Object.assign(j, { busy: false }));
    state.jobs.sort((a, b) => (b.created || 0) - (a.created || 0));
    // Drop ZIP selections whose job is gone or no longer embedded.
    state.zipSel = new Set(Array.from(state.zipSel).filter((id) => {
      const j = jobById(id);
      return j && j.has_embedded;
    }));
    // Drop streams for jobs that no longer exist server-side.
    // Null-safe: a stale `null` entry (terminal close) must not throw here —
    // a throw would abort the rest of loadJobs() (no render, no re-subscribe).
    Object.keys(state.es).forEach((id) => {
      const src = state.es[id];
      if (src) src.close();
      delete state.es[id];
    });
    renderJobs();
    updateZipButton();
    // Subscribe to every still-running job with its own EventSource.
    state.jobs.forEach((j) => {
      if (RUNNING_STATUSES.has(j.status)) connectStream(j.id);
    });
    setGlobalStatus();
  } catch (e) {
    setStatus("offline", "error");
  }
}

function renderJobs() {
  const section = $("#jobs-section");
  const list = $("#jobs-list");
  section.classList.toggle("hidden", state.jobs.length === 0);
  list.innerHTML = "";
  state.jobs.forEach((j) => list.appendChild(jobCard(j)));
}

function jobCard(job) {
  const card = el("div", "job-card" + (state.sel && state.sel.jobId === job.id ? " selected" : ""));
  card.dataset.jid = job.id;

  const head = el("div", "job-head");
  const title = el("div", "job-title");
  title.appendChild(el("span", "job-filename", job.filename));
  const pcls = RUNNING_STATUSES.has(job.status) ? "running"
    : (job.status === "done" || job.status === "embedded") ? "done"
    : job.status === "error" ? "error" : "";
  title.appendChild(el("span", "pill" + (pcls ? " " + pcls : ""),
                       t(STATUS_LABEL[job.status] || job.status)));
  head.appendChild(title);
  // During the pre-OCR render phase the bar reflects render progress; the
  // page-level counts are shown again once OCR itself starts.
  const rendering = !!(job.render && job.render.total > 0 && job.render.current < job.render.total);
  const countLabel = rendering
    ? "⏳ " + job.render.current + " / " + job.render.total
    : job.current + " / " + (job.total || "?");
  head.appendChild(el("span", "job-count", countLabel));
  card.appendChild(head);

  const bar = el("div", "bar");
  const fill = el("div", "fill");
  fill.style.width = rendering
    ? Math.round((job.render.current / job.render.total) * 100) + "%"
    : (job.total ? Math.round((job.current / job.total) * 100) + "%" : "2%");
  bar.appendChild(fill);
  card.appendChild(bar);

  // Export progress: the LLM fix-up / figure extraction / chapter split
  // stream their own progress through the SSE export route (the phases can
  // take minutes; reflow is instant).
  if (job.export) {
    const wrap = el("div", "export-progress");
    const phase = job.export.phase;
    const label = phase === "reflow" ? t("job.exportReflow")
      : phase === "images" ? t("job.exportPhaseImages")
      : phase === "zip" ? t("job.exportPhaseZip")
      : phase === "outline" ? t("job.exportOutline")
      : t("job.exportProgress", { done: job.export.done, total: job.export.total });
    wrap.appendChild(el("span", "hint", "⬇ " + label));
    const ebar = el("div", "bar");
    const efill = el("div", "fill");
    const etotal = job.export.total || 0;
    efill.style.width = (phase === "llm" || phase === "outline"
                         || phase === "images") && etotal
      ? Math.min(100, Math.round((job.export.done / etotal) * 100)) + "%" : "8%";
    ebar.appendChild(efill);
    wrap.appendChild(ebar);
    card.appendChild(wrap);
  }

  if (job.error) card.appendChild(el("div", "job-err", "✗ " + job.error));

  const actions = el("div", "job-actions");
  const active = RUNNING_STATUSES.has(job.status);

  if (active) {
    const stop = el("button", "warn", job.busy ? t("job.stopping") : t("job.stop"));
    stop.disabled = !!job.busy;
    stop.onclick = () => stopJob(job.id);
    actions.appendChild(stop);
  } else {
    // Start / resume: every inactive job that is not already complete gets a
    // run button.  A stopped/recovered job with zero *reported* pages must
    // still be startable — the backend keeps its completed pages on disk and
    // the retry endpoint resumes exactly the pages that are missing.
    const complete = job.status === "done" || job.status === "embedded";
    if (!complete) {
      const started = job.current > 0;
      const retry = el("button", "primary",
        started ? t("job.retryRemaining")
                : job.status === "error" ? t("job.retry") : t("job.start"));
      retry.disabled = !!job.busy;
      retry.onclick = () => retryJob(job.id);
      actions.appendChild(retry);
    }
    if (job.current > 0) {
      const partial = el("button", "primary", t("job.downloadPartial"));
      partial.disabled = !!job.busy;
      partial.onclick = () => partialJob(job.id);
      actions.appendChild(partial);
    }
    const clear = el("button", "warn", t("job.clear"));
    clear.onclick = () => clearJob(job.id);
    actions.appendChild(clear);
  }

  if (job.current > 0) {
    const edit = el("button", "small", t("job.editPages"));
    edit.onclick = () => selectJob(job.id);
    actions.appendChild(edit);
  }
  if (job.has_embedded) {
    const zipSel = el("label", "job-zip-sel");
    zipSel.title = t("job.zipSelect");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.zipSel.has(job.id);
    cb.onchange = () => toggleZipSel(job.id, cb.checked);
    zipSel.appendChild(cb);
    actions.appendChild(zipSel);
    const a = el("a", "download-link", t("job.embeddedPdf"));
    a.href = `/api/download/${job.id}.pdf`;
    a.download = "";
    actions.appendChild(a);
  }
  // Other-format export: markdown / LaTeX links carrying the LLM options.
  // Without any option the link is a plain instant download (reflow only);
  // with an option checked the click routes through the SSE stream, which
  // streams a progress bar (LLM passes, figure extraction, chapter split)
  // and the job card shows it.
  if (job.current > 0 && !job.exporting) {
    const opts = el("details", "export-opts");
    opts.appendChild(el("summary", null, t("job.exportOptions")));
    opts.appendChild(_exportOptCheckbox("export-opt-blocks", "job.exportLlmBlocks",
                                        "blocks"));
    opts.appendChild(_exportOptCheckbox("export-opt-outline", "job.exportLlmOutline",
                                        "outline"));
    opts.appendChild(_exportImagesSelect());
    opts.appendChild(_exportSplitCheckbox("export-opt-split", "job.exportSplit"));
    actions.appendChild(opts);
    const imagesQ = (state.exportImages && state.exportImages !== "none")
      ? `?images=${state.exportImages}` : "";
    const md = el("a", "download-link small", t("job.exportMd"));
    md.href = `/api/export/${job.id}.md${imagesQ}`;
    md.download = "";
    md.title = t("job.exportMdTitle");
    md.addEventListener("click", (ev) => {
      const useStream = state.exportLlm.blocks || state.exportLlm.outline
        || state.exportSplit || state.exportImages !== "none";
      if (useStream) {
        // Anything beyond a plain reflow needs the server to do real work
        // (LLM passes, figure crops, chapter split): stream it so the user
        // sees progress instead of a silent wait.
        ev.preventDefault();
        exportWithLlm(job.id, "md");
      }
    });
    actions.appendChild(md);
    const tex = el("a", "download-link small", t("job.exportTex"));
    tex.href = `/api/export/${job.id}.tex`;
    tex.download = "";
    tex.title = t("job.exportTexTitle");
    tex.addEventListener("click", (ev) => {
      if (state.exportLlm.blocks || state.exportLlm.outline) {
        ev.preventDefault();
        exportWithLlm(job.id, "tex");
      }
    });
    actions.appendChild(tex);
  }
  card.appendChild(actions);
  return card;
}

function connectStream(jobId) {
  if (state.es[jobId]) state.es[jobId].close();
  const es = new EventSource(`/api/ocr/stream/${jobId}`);
  state.es[jobId] = es;
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    applyJobEvent(jobId, msg);
  };
  // On connection trouble the browser auto-reconnects; the server re-synthesizes
  // terminal events, so a reconnect always catches the job up. Do nothing here.
  es.onerror = () => {};
}

/* ---------- export (markdown / LaTeX, opt-in LLM post-processing) ---------- */

function _exportCheckbox(id, i18nKey, isOn, onChange) {
  const label = el("label", "inline-check");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.id = id;
  cb.checked = !!isOn();
  cb.onchange = () => onChange(cb.checked);
  label.appendChild(cb);
  const span = document.createElement("span");
  span.setAttribute("data-i18n", i18nKey);
  span.textContent = t(i18nKey);
  label.appendChild(span);
  return label;
}

function _exportOptCheckbox(id, i18nKey, key) {
  return _exportCheckbox(id, i18nKey,
                         () => state.exportLlm[key],
                         (v) => { state.exportLlm[key] = v; });
}

function _exportSplitCheckbox(id, i18nKey) {
  return _exportCheckbox(id, i18nKey,
                         () => state.exportSplit,
                         (v) => { state.exportSplit = v; });
}

// Image embedding select (markdown only): none | zip | base64.
function _exportImagesSelect() {
  const sel = el("select", "export-images-sel");
  sel.title = t("job.exportImagesTitle");
  [["none", "job.exportImagesNone"],
   ["zip", "job.exportImagesZip"],
   ["base64", "job.exportImagesBase64"]].forEach(([value, key]) => {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = t(key);
    if (state.exportImages === value) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = () => { state.exportImages = sel.value; };
  return sel;
}

function exportWithLlm(jobId, ext) {
  const job = jobById(jobId);
  if (!job || job.exporting) return;
  const params = [];
  if (state.exportLlm.blocks) params.push("llm_blocks=1");
  if (state.exportLlm.outline) params.push("llm_outline=1");
  if (ext === "md" && state.exportImages && state.exportImages !== "none") {
    params.push("images=" + state.exportImages);
  }
  if (ext === "md" && state.exportSplit) {
    params.push("split=1");
  }
  if (!params.length) return;  // nothing selected: plain link handles it
  job.exporting = true;
  job.export = { phase: "reflow", done: 0, total: 0 };
  renderJobs();
  const es = new EventSource(`/api/export/stream/${jobId}.${ext}?${params.join("&")}`);
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "progress") {
      job.export = { phase: msg.phase, done: msg.done || 0, total: msg.total || 0 };
      renderJobs();
    } else if (msg.type === "done") {
      es.close();
      job.exporting = false;
      job.export = null;
      renderJobs();
      if (msg.zip_url) {
        // Multi-file exports (zip / split) are served through a single-use
        // download link instead of inline text.
        const a = document.createElement("a");
        a.href = msg.zip_url;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } else {
        downloadText(`${(job.filename || jobId).replace(/\.pdf$/i, "")}.${ext}`,
                     msg.text,
                     ext === "tex" ? "application/x-tex" : "text/markdown");
      }
      toast(t("job.exportDone"), "success");
    } else if (msg.type === "error") {
      es.close();
      job.exporting = false;
      job.export = null;
      renderJobs();
      toast(t("job.exportFailed", { msg: msg.message }), "error");
    }
  };
  // The stream ends after done/error (handled above); an unexpected
  // connection drop must NOT auto-reconnect into a fresh export run.
  es.onerror = () => {
    es.close();
    if (job.exporting) {
      job.exporting = false;
      job.export = null;
      renderJobs();
      toast(t("job.exportFailed", { msg: "connection lost" }), "error");
    }
  };
}

function downloadText(filename, text, media) {
  const blob = new Blob([text], { type: `${media};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function applyJobEvent(jobId, msg) {
  const job = jobById(jobId);
  if (!job) return;
  const prevCurrent = job.current;

  if (msg.type === "progress") {
    if (msg.phase === "render") {
      // Pre-OCR rasterization phase: advance the bar with render counts and
      // keep the page-level current/total untouched (they only count OCR).
      job.render = { current: msg.current, total: msg.total };
    } else {
      job.render = null;
      // Completed-page COUNT, not the page number: the server streams `current`
      // and `pages_done` as the running count of finished pages (page identity
      // lives in `page_index`). Prefer the explicit count when present.
      const pagesDone = typeof msg.pages_done === "number" ? msg.pages_done : msg.current;
      Object.assign(job, {
        status: RUNNING_STATUSES.has(msg.status) ? msg.status : job.status,
        current: pagesDone,
        total: msg.total,
        error: null,
      });
    }
  } else if (msg.type === "status") {
    job.render = null;
    const done = (msg.result || []).filter(Boolean).length;
    Object.assign(job, {
      status: msg.status,
      error: null,
      current: (msg.status === "done" || msg.status === "stopped") ? done : (msg.result ? done : job.current),
    });
  } else if (msg.type === "error") {
    Object.assign(job, { status: "error", error: msg.message });
  } else {
    return; // warning / error_page — cosmetic, nothing persisted
  }

  const terminal = job.status === "done" || job.status === "stopped" || job.status === "error";
  if (terminal && state.es[jobId]) {
    state.es[jobId].close();
    delete state.es[jobId];  // never leave a stale null entry in the map
  }
  renderJobs();

  // Keep the open editor in sync with its job (new pages appearing live).
  if (state.sel && state.sel.jobId === jobId) {
    if (msg.type === "status" || (msg.type === "progress" && msg.current !== prevCurrent)) {
      refreshSelectedPages();
    }
  }
}

/* ---------- per-job actions ---------- */
async function stopJob(jobId) {
  const job = jobById(jobId);
  if (!job || job.busy || !RUNNING_STATUSES.has(job.status)) return;
  job.busy = true;
  renderJobs();
  try {
    await api(`/api/ocr/stop/${jobId}`, { method: "POST" });
    const data = await api(`/api/pages/${jobId}`);
    Object.assign(job, {
      status: "stopped",
      current: (data.pages || []).filter(Boolean).length,
      total: data.total || job.total,
    });
  } catch (e) {
    Object.assign(job, { status: "error", error: t("job.stopFailed", { msg: e.message }) });
  } finally {
    job.busy = false;
    renderJobs();
  }
}

async function retryJob(jobId) {
  const job = jobById(jobId);
  if (!job || job.busy) return;
  job.busy = true;
  renderJobs();
  const fd = new FormData();
  const cfg = currentEngineCfg();
  fd.append("ocr_engine", cfg.ocr_engine);
  if (cfg.lang) fd.append("lang", cfg.lang);
  const c = Math.max(1, Math.min(32, parseInt($("#concurrency").value || "1", 10)));
  fd.append("concurrency", String(c));
  // Page range (1-based inclusive) + force flag, off the shared controls.
  const range = selectedPageRange();
  if (range) {
    fd.append("page_start", String(range.start));
    if (range.end !== null) fd.append("page_end", String(range.end));
  }
  if ($("#retry-force") && $("#retry-force").checked) fd.append("force", "true");
  try {
    await api(`/api/ocr/retry/${jobId}`, { method: "POST", body: fd });
    Object.assign(job, { status: "retrying", error: null });
    renderJobs();
    connectStream(jobId);
  } catch (e) {
    Object.assign(job, { status: "error", error: t("job.retryFailed", { msg: e.message }) });
    renderJobs();
  } finally {
    job.busy = false;
    renderJobs();
  }
}

/* Read the shared page-range controls (upload-opts) and validate them.
   Returns null when no range is selected (retry everything missing), or
   {start, end} (1-based inclusive) when a valid range is set. */
function selectedPageRange() {
  const startEl = $("#page-start");
  const endEl = $("#page-end");
  if (!startEl || !endEl) return null;
  const start = startEl.value === "" ? null : parseInt(startEl.value, 10);
  const end = endEl.value === "" ? null : parseInt(endEl.value, 10);
  if (start == null && end == null) return null;
  const s = start == null ? 1 : start;
  // end may stay null (open-ended -> the server fills up to the doc length).
  const e = end === null ? null : end;
  if (!Number.isFinite(s) || s < 1 || (e !== null && (e < 1 || s > e))) {
    return null;  // invalid — caller should skip sending the range
  }
  return { start: s, end: e };
}

/* Show a live hint for the shared page-range controls and flag invalid values
   (start>end, or out of range).  Invalid ranges are surfaced but do not break
   retry — retryJob simply falls back to "retry missing pages". */
function updatePageRangeHint() {
  const hintEl = $("#page-range-hint");
  if (!hintEl) return;
  const startEl = $("#page-start");
  const endEl = $("#page-end");
  if (!startEl || !endEl) return;
  const s = startEl.value === "" ? null : parseInt(startEl.value, 10);
  const e = endEl.value === "" ? null : parseInt(endEl.value, 10);
  if (s == null && e == null) { hintEl.textContent = ""; return; }

  const sv = s == null ? 1 : s;
  const invalidStart = !Number.isFinite(sv) || sv < 1;
  const invalidEnd = e !== null && (!Number.isFinite(e) || e < 1);
  const reversed = s !== null && e !== null && s > e;
  if (invalidStart || invalidEnd || reversed) {
    hintEl.textContent = t("upload.pageRangeInvalid");
    return;
  }
  if (e === null) {
    hintEl.textContent = t("upload.pageRangeOpenStart", { start: sv });
  } else if (s === null) {
    hintEl.textContent = t("upload.pageRangeOpenEnd", { end: e });
  } else {
    hintEl.textContent = t("upload.pageRangeOk", { start: s, end: e });
  }
}

/* Re-run OCR for just the currently-viewed page, forcing it even if it
   already has a result — the A/B test path after switching prompt/engine. */
async function reOcrPage() {
  const sel = state.sel;
  if (!sel) return;
  const page = sel.pages && sel.pages[sel.pageIndex];
  if (!page) return;
  const jobId = sel.jobId;
  const job = jobById(jobId);
  if (!job || job.busy) return;
  // The backend refuses retries while this job's OCR is still running (the
  // page may already have a result, but the run has not finished).  Tell the
  // user up front instead of surfacing a confusing 409 from the API.
  if (RUNNING_STATUSES.has(job.status)) {
    toast(t("job.runningNoReOcr"), "error");
    return;
  }
  const pageNo = page.page_index + 1;  // 1-based, user-facing
  job.busy = true;
  renderJobs();
  const fd = new FormData();
  const cfg = currentEngineCfg();
  fd.append("ocr_engine", cfg.ocr_engine);
  if (cfg.lang) fd.append("lang", cfg.lang);
  fd.append("page_start", String(pageNo));
  fd.append("page_end", String(pageNo));
  fd.append("force", "true");
  try {
    await api(`/api/ocr/retry/${jobId}`, { method: "POST", body: fd });
    Object.assign(job, { status: "retrying", error: null });
    renderJobs();
    connectStream(jobId);
  } catch (e) {
    Object.assign(job, { status: "error", error: t("job.retryFailed", { msg: e.message }) });
    renderJobs();
  } finally {
    job.busy = false;
    renderJobs();
  }
}

async function partialJob(jobId) {
  const job = jobById(jobId);
  if (!job || job.busy) return;
  job.busy = true;
  renderJobs();
  try {
    const data = await api(`/api/pages/${jobId}`);
    const pages = (data.pages || []).filter(Boolean);
    if (!pages.length) throw new Error(t("job.noPages"));
    const out = await api(`/api/embed/${jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, pages }),
    });
    window.open(out.url, "_blank");
  } catch (e) {
    Object.assign(job, { error: t("job.partialFailed", { msg: e.message }) });
  } finally {
    job.busy = false;
    renderJobs();
  }
}

async function clearJob(jobId) {
  const job = jobById(jobId);
  if (!job) return;
  if (!confirm(t("job.clearConfirm", { name: job.filename }))) {
    return;
  }
  try {
    await api(`/api/ocr/clear/${jobId}`, { method: "POST" });
  } catch (e) { /* job may already be gone — still reset the UI */ }
  if (state.es[jobId]) { state.es[jobId].close(); delete state.es[jobId]; }
  state.jobs = state.jobs.filter((j) => j.id !== jobId);
  state.zipSel.delete(jobId);
  updateZipButton();
  if (state.sel && state.sel.jobId === jobId) setSelectedJob(null);
  renderJobs();
  setGlobalStatus();
}

/* ---------- selected-job editor ---------- */
async function selectJob(jobId) {
  state.sel = {
    jobId, pages: [], pageIndex: 0, embedded: false,
    // (#6) per-session editor extras shared across pages/handlers:
    selection: { pageIndex: -1, indices: new Set() },  // selected block indices for a page
    undoStack: [],       // snapshots of page.blocks before structural edits
    drawMode: false,     // "draw a new block on the overlay" mode
    pendingDraw: null,   // live rectangle while drawing a new block
  };
  const job = jobById(jobId);
  const label = $("#editing-job");
  if (label) label.textContent = job ? t("workspace.editing", { name: job.filename }) : "";
  $("#download-link").classList.add("hidden");
  $("#embed-status").textContent = "";
  // The report belongs to the previous job's embed — hide it on selection.
  hideReport();
  $("#report-status").textContent = "";
  renderJobs();
  $("#workspace").classList.remove("hidden");
  await refreshSelectedPages();
}

function setSelectedJob(sel) {
  state.sel = sel;
  $("#workspace").classList.toggle("hidden", !sel);
  if (!sel) {
    $("#editing-job").textContent = "";
    $("#preview-img").removeAttribute("src");
    $("#blocks").innerHTML = "";
    $("#download-link").classList.add("hidden");
    $("#embed-status").textContent = "";
    hideReport();
  }
  renderJobs();
}

async function refreshSelectedPages() {
  const sel = state.sel;
  if (!sel) return;
  try {
    const data = await api(`/api/pages/${sel.jobId}`);
    const pages = (data.pages || []).filter(Boolean);
    sel.status = data.status;
    sel.pages = pages;
    // Fresh server data replaces the page objects — block-edit undo snapshots
    // would point at stale references, so drop them (undo is per-session only).
    if (sel.undoStack) sel.undoStack.length = 0;
    const job = jobById(sel.jobId);
    if (job) Object.assign(job, { current: pages.length, total: data.total || job.total });
    $("#btn-embed").disabled = !pages.length;
    const vBtn = $("#btn-validate");
    if (vBtn) vBtn.disabled = !data.has_embedded;
    if (!pages.length) {
      $("#blocks").innerHTML = "";
      $("#blocks").appendChild(
        el("div", "embed-hint", t("editor.noPagesYet")));
      return;
    }
    if (sel.pageIndex >= pages.length) sel.pageIndex = pages.length - 1;
    renderTabs();
    renderPage();
  } catch (e) {
    // Only close the editor if the job is really gone; a transient network
    // error shouldn't kick the user out of the workspace.
    if (e && (e.status === 404 || /not found/i.test(String(e.message)))) {
      setSelectedJob(null);
    }
  }
}

function renderTabs() {
  const wrap = $("#page-tabs");
  wrap.innerHTML = "";
  const sel = state.sel;
  if (!sel) return;
  sel.pages.forEach((pg, i) => {
    // Label the tab with the REAL PDF page number (page_index), not the
    // position in this list.  The server only sends pages that have a result,
    // so with missing pages (failed OCR, not-yet-done in a running job) the
    // two diverge, and position labels (1,2,3,…) mislabel e.g. pages 1,3,5.
    // Position `i` still drives navigation/selection; only the shown number is
    // the actual page.
    const pageNo = (pg.page_index ?? 0) + 1;
    const tab = el("button", "page-tab" + (i === sel.pageIndex ? " active" : ""), String(pageNo));
    const low = pageLowConfCount(pg);
    if (low) {
      tab.appendChild(el("span", "tab-badge", String(low)));
    }
    const tip = t("editor.pdfPage", { n: pageNo });
    tab.title = low
      ? tip + " · " + t("editor.confPageBadge", { n: low, p: state.confThreshold })
      : tip;
    tab.onclick = () => { sel.pageIndex = i; renderTabs(); renderPage(); };
    wrap.appendChild(tab);
  });
  $("#btn-prev").disabled = sel.pageIndex === 0;
  $("#btn-next").disabled = sel.pageIndex >= sel.pages.length - 1;
}

function renderPage() {
  const sel = state.sel;
  const reocrBtn = $("#btn-reocr");
  if (!sel) {
    if (reocrBtn) reocrBtn.disabled = true;
    return;
  }
  const page = sel.pages[sel.pageIndex];
  if (reocrBtn) reocrBtn.disabled = !page;
  if (!page) return;

  const img = $("#preview-img");
  img.src = `/api/pages/${sel.jobId}/${page.page_index}/image?ts=${Date.now()}`;
  img.onload = () => drawOverlay(page);
  img.onerror = () => {};

  state.sourceW = page.width;
  state.sourceH = page.height;

  // Prune stale block selections when page/blocks change (#6).
  sanitizeSelection();

  // Editor blocks (+ block-ops toolbar: Merge / Add block / Undo)
  const blocksBox = $("#blocks");
  blocksBox.innerHTML = "";
  blocksBox.appendChild(renderBlockOps());
  if (!page.blocks || page.blocks.length === 0) {
    blocksBox.appendChild(el("div", "", t("editor.noBlocks")));
  }
  let visibleBlocks = 0;
  (page.blocks || []).forEach((block, bi) => {
    if (state.confFilter && !isLowConf(block)) return;
    visibleBlocks++;
    blocksBox.appendChild(buildBlockEditor(block, bi));
  });
  if (state.confFilter && visibleBlocks === 0
      && page.blocks && page.blocks.length) {
    blocksBox.appendChild(el("div", "embed-hint",
      t("editor.confNoLowOnPage", { p: state.confThreshold })));
  }

  // Confidence summary line
  const count = $("#conf-count");
  if (count) {
    if (!jobHasConfData()) {
      count.textContent = t("editor.confNoData");
    } else {
      const total = state.sel.pages.reduce((n, pg) => n + pageLowConfCount(pg), 0);
      count.textContent = total
        ? t("editor.confCount", { n: total, p: state.confThreshold })
        : t("editor.confNone", { p: state.confThreshold });
    }
  }

  $("#zoom-label").textContent = state.zoom + "%";
  img.style.width = state.zoom + "%";
}

function drawOverlay(page) {
  const canvas = $("#overlay-canvas");
  const img = $("#preview-img");
  if (!img.complete || !img.naturalWidth) return;
  const rect = img.getBoundingClientRect();
  canvas.width = rect.width * devicePixelRatio;
  canvas.height = rect.height * devicePixelRatio;
  const ctx = canvas.getContext("2d");
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const sx = rect.width / page.width;
  const sy = rect.height / page.height;

  (page.blocks || []).forEach((block, bi) => {
    const [x1, y1, x2, y2] = block.bbox;
    let color = block.kind === "image" ? "#7a5cff" : "#2f6fed";
    if (block.kind !== "image") {
      const cls = confClass(confPct(block));
      if (cls === "conf-low") color = "#e85d3a";
      else if (cls === "conf-med") color = "#f0a020";
    }
    const selected = isBlockSelected(bi);
    // Selected blocks get a soft fill + accent stroke + a resize handle.
    if (selected) {
      ctx.fillStyle = "rgba(47, 111, 237, 0.12)";
      ctx.fillRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
      color = "#2f6fed";
      ctx.lineWidth = 3;
    } else {
      ctx.lineWidth = 2;
    }
    ctx.strokeStyle = color;
    ctx.strokeRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
    if (selected) {
      // Bottom-right resize handle.
      const h = 9;
      ctx.fillStyle = "#2f6fed";
      ctx.fillRect(x2 * sx - h, y2 * sy - h, h * 2, h * 2);
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x2 * sx - h, y2 * sy - h, h * 2, h * 2);
    }
  });

  // Live rectangle while drawing a new block (drag on the overlay).
  const pending = state.sel && state.sel.pendingDraw;
  if (pending) {
    const [x1, y1, x2, y2] = pending;
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = "#2f6fed";
    ctx.lineWidth = 2;
    ctx.strokeRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
    ctx.restore();
  }
}

function buildBlockEditor(block, bi) {
  const wrapper = el("div", "block");
  wrapper.dataset.bi = bi;
  // (#6) keyboard focus enables arrow-key bbox nudge (shift = 10px).
  wrapper.tabIndex = 0;
  if (isBlockSelected(bi)) wrapper.classList.add("selected");
  const pct = confPct(block);
  if (confClass(pct) === "conf-low") wrapper.classList.add("block-low");
  const meta = el("div", "meta");
  meta.appendChild(el("span", "badge", block.kind));
  meta.appendChild(el("span", "conf-badge " + confClass(pct),
    pct === null ? t("editor.confNa") : Math.round(pct) + "%"));
  meta.appendChild(el("div", "coords", block.bbox.join(", ") + " px"));
  // (#6) Click the meta row to toggle block selection (for merge / overlay ops).
  meta.classList.add("selectable");
  meta.title = t("editor.mergeTitle");
  meta.onclick = (e) => {
    e.stopPropagation();
    toggleBlockSelect(bi);
  };

  const textarea = document.createElement("textarea");
  textarea.value = block.text || block.caption || "";
  textarea.placeholder = t("editor.placeholder");
  textarea.oninput = (e) => {
    textarea.classList.add("edit");
    const sel = state.sel;
    if (!sel) return;
    const page = sel.pages[sel.pageIndex];
    const target = page.blocks[bi];
    if (target) target.text = e.target.value;
    sel.embedded = false;
    setStatus("dirty", "running");
  };

  // (#6) per-block ops row: Split (at textarea caret) + delete.
  const opsRow = el("div", "ops-row");
  const split = el("button", "small", t("editor.split"));
  split.title = t("editor.splitTitle");
  split.onclick = (e) => {
    e.stopPropagation();
    splitBlock(bi);
  };
  opsRow.appendChild(split);
  const del = el("button", "del", "✕");
  del.title = t("editor.removeBlock");
  del.onclick = () => {
    const sel = state.sel;
    if (!sel) return;
    pushUndo();
    sel.pages[sel.pageIndex].blocks.splice(bi, 1);
    if (sel.selection) sel.selection.indices.delete(bi);
    sel.embedded = false;
    setStatus("dirty", "running");
    renderPage();
  };
  opsRow.appendChild(del);

  // (#6) arrow-key bbox nudge when this block editor is focused.
  wrapper.addEventListener("keydown", (e) => {
    const moves = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    const mv = moves[e.key];
    if (!mv) return;
    e.preventDefault();
    e.stopPropagation();
    const page = currentPage();
    const sel = state.sel;
    if (!page || !sel) return;
    const b = page.blocks[bi];
    if (!b) return;
    if (!wrapper.dataset.nudgeUndone) {
      pushUndo();
      wrapper.dataset.nudgeUndone = "1";
    }
    const step = e.shiftKey ? 10 : 1;
    b.bbox = clampBbox([
      b.bbox[0] + mv[0] * step, b.bbox[1] + mv[1] * step,
      b.bbox[2] + mv[0] * step, b.bbox[3] + mv[1] * step,
    ], page);
    sel.embedded = false;
    setStatus("dirty", "running");
    updateBlockCoords(bi);
    drawOverlay(page);
  });
  // Reset the nudge-undo latch when focus leaves this block editor.
  wrapper.addEventListener("focusout", () => { wrapper.dataset.nudgeUndone = ""; });

  // ---- Interactive font-size control (debug: too big / too small) ----
  if (block.font_scale == null) block.font_scale = 1.0;
  const fsRow = el("div", "fs-row");
  const fsLabel = el("span", "fs-label", t("editor.fontSize"));
  const fsSlider = document.createElement("input");
  fsSlider.type = "range";
  fsSlider.min = "0.5"; fsSlider.max = "1.5"; fsSlider.step = "0.05";
  fsSlider.value = block.font_scale;
  fsSlider.className = "fs-slider";
  const fsVal = el("span", "fs-val", block.font_scale.toFixed(2) + "×");
  fsSlider.oninput = () => {
    block.font_scale = parseFloat(fsSlider.value);
    fsVal.textContent = block.font_scale.toFixed(2) + "×";
    if (state.sel) state.sel.embedded = false;
    setStatus("dirty", "running");
  };
  // Reset to auto
  const fsReset = el("button", "small", t("editor.auto"));
  fsReset.title = t("editor.autoTitle");
  fsReset.onclick = () => {
    block.font_scale = 1.0;
    fsSlider.value = "1.0";
    fsVal.textContent = "1.00×";
    if (state.sel) state.sel.embedded = false;
    setStatus("dirty", "running");
  };
  fsRow.appendChild(fsLabel);
  fsRow.appendChild(fsSlider);
  fsRow.appendChild(fsVal);
  fsRow.appendChild(fsReset);
  fsRow.appendChild(el("span", "fs-der", ""));

  wrapper.appendChild(meta);
  wrapper.appendChild(textarea);
  wrapper.appendChild(opsRow);
  wrapper.appendChild(fsRow);
  return wrapper;
}

/* ---------- block operations (#6): select / merge / split / add / undo ---------- */
function currentPage() {
  const sel = state.sel;
  if (!sel || !sel.pages || !sel.pages.length) return null;
  return sel.pages[sel.pageIndex] || null;
}

function cloneBlocks(blocks) {
  return (blocks || []).map((b) => ({ ...b, bbox: [...b.bbox] }));
}

function pushUndo() {
  const sel = state.sel;
  const page = currentPage();
  if (!sel || !page) return;
  sel.undoStack.push({ pageIndex: sel.pageIndex, blocks: cloneBlocks(page.blocks) });
  if (sel.undoStack.length > 50) sel.undoStack.shift();
}

function undoLast() {
  const sel = state.sel;
  const snap = sel && sel.undoStack.pop();
  if (!snap) return;
  const page = currentPage();
  if (!page || sel.pageIndex !== snap.pageIndex) {
    // Snapshot belongs to a different page — push it back and keep going.
    sel.undoStack.push(snap);
    return;
  }
  page.blocks = cloneBlocks(snap.blocks);
  sel.embedded = false;
  setStatus("dirty", "running");
  renderPage();
  toast(t("editor.undoDone"), "success");
}

function sanitizeSelection() {
  const sel = state.sel;
  const page = currentPage();
  if (!sel || !page) return;
  if (sel.selection.pageIndex !== sel.pageIndex) {
    sel.selection = { pageIndex: sel.pageIndex, indices: new Set() };
    return;
  }
  const total = (page.blocks || []).length;
  for (const i of [...sel.selection.indices]) {
    if (i < 0 || i >= total) sel.selection.indices.delete(i);
  }
}

function isBlockSelected(bi) {
  const sel = state.sel;
  return !!(sel && sel.selection &&
    sel.selection.pageIndex === sel.pageIndex && sel.selection.indices.has(bi));
}

function toggleBlockSelect(bi) {
  const sel = state.sel;
  if (!sel) return;
  if (sel.selection.pageIndex !== sel.pageIndex) {
    sel.selection = { pageIndex: sel.pageIndex, indices: new Set() };
  }
  if (sel.selection.indices.has(bi)) sel.selection.indices.delete(bi);
  else sel.selection.indices.add(bi);
  renderPage();
}

function selectedIndices() {
  const sel = state.sel;
  if (!sel || !sel.selection || sel.selection.pageIndex !== sel.pageIndex) return [];
  return [...sel.selection.indices];
}

function updateBlockCoords(bi) {
  const blockEl = document.querySelector(`.block[data-bi="${bi}"] .coords`);
  const page = currentPage();
  const b = page && page.blocks[bi];
  if (blockEl && b) blockEl.textContent = b.bbox.join(", ") + " px";
}

function renderBlockOps() {
  const sel = state.sel;
  const bar = el("div", "block-ops");
  const selected = selectedIndices();

  const merge = el("button", "small", t("editor.merge"));
  merge.title = t("editor.mergeTitle");
  merge.disabled = selected.length < 2;
  merge.onclick = mergeSelected;
  bar.appendChild(merge);

  const add = el("button", "small" + (sel && sel.drawMode ? " active" : ""), t("editor.addBlock"));
  add.title = t("editor.addBlockTitle");
  add.onclick = toggleDrawMode;
  bar.appendChild(add);

  const undo = el("button", "small", t("editor.undo"));
  undo.title = t("editor.undoTitle");
  undo.disabled = !(sel && sel.undoStack && sel.undoStack.length);
  undo.onclick = () => undoLast();
  bar.appendChild(undo);

  if (sel && sel.drawMode) {
    bar.appendChild(el("span", "hint ops-hint", t("editor.addBlockHint")));
  }
  return bar;
}

function mergeSelected() {
  const sel = state.sel;
  const page = currentPage();
  if (!sel || !page) return;
  const indices = selectedIndices().sort((a, b) => a - b);
  if (indices.length < 2) return;
  const blocks = page.blocks || [];
  const chosen = indices.map((i) => blocks[i]).filter(Boolean);
  if (chosen.length < 2) return;
  const bbox = [
    Math.min(...chosen.map((b) => b.bbox[0])),
    Math.min(...chosen.map((b) => b.bbox[1])),
    Math.max(...chosen.map((b) => b.bbox[2])),
    Math.max(...chosen.map((b) => b.bbox[3])),
  ];
  const merged = {
    kind: chosen[0].kind,
    bbox,
    text: chosen.map((b) => (b.text || b.caption || "")).filter((s) => s).join("\n"),
    caption: "",
    conf: chosen[0].conf,
    font_scale: chosen[0].font_scale != null ? chosen[0].font_scale : 1.0,
  };
  pushUndo();
  const newBlocks = blocks.filter((_, i) => !indices.includes(i));
  newBlocks.push(merged);
  page.blocks = newBlocks;
  sel.embedded = false;
  setStatus("dirty", "running");
  sel.selection = { pageIndex: sel.pageIndex, indices: new Set([newBlocks.length - 1]) };
  toast(t("editor.mergeDone", { n: chosen.length }), "success");
  renderPage();
}

function splitBlock(bi) {
  const sel = state.sel;
  const page = currentPage();
  if (!sel || !page) return;
  const blocks = page.blocks || [];
  const block = blocks[bi];
  if (!block) return;
  const ta = document.querySelector(`.block[data-bi="${bi}"] textarea`);
  const pos = ta ? ta.selectionStart : -1;
  const text = block.text || "";
  if (pos <= 0 || pos >= text.length) {
    toast(t("editor.splitNoop"), "info");
    return;
  }
  const [x1, y1, x2, y2] = block.bbox;
  const frac = Math.min(Math.max(pos / Math.max(text.length, 1), 0), 1);
  const mk = (t1, bx) => ({
    kind: block.kind, bbox: bx, text: t1, caption: "",
    conf: block.conf, font_scale: block.font_scale != null ? block.font_scale : 1.0,
  });
  let parts;
  if ((x2 - x1) >= (y2 - y1)) {
    const midX = Math.min(Math.max(Math.round(x1 + (x2 - x1) * frac), x1 + 1), x2 - 1);
    parts = [mk(text.slice(0, pos), [x1, y1, midX, y2]), mk(text.slice(pos), [midX, y1, x2, y2])];
  } else {
    const midY = Math.min(Math.max(Math.round(y1 + (y2 - y1) * frac), y1 + 1), y2 - 1);
    parts = [mk(text.slice(0, pos), [x1, y1, x2, midY]), mk(text.slice(pos), [x1, midY, x2, y2])];
  }
  pushUndo();
  page.blocks = [...blocks.slice(0, bi), ...parts, ...blocks.slice(bi + 1)];
  sel.embedded = false;
  setStatus("dirty", "running");
  sel.selection = { pageIndex: sel.pageIndex, indices: new Set([bi, bi + 1]) };
  toast(t("editor.splitDone"), "success");
  renderPage();
}

function toggleDrawMode() {
  const sel = state.sel;
  if (!sel) return;
  sel.drawMode = !sel.drawMode;
  if (!sel.drawMode) sel.pendingDraw = null;
  renderPage();
}

/* ---- interactive overlay: move / resize / draw-new-block ---- */
let dragState = null;

function clampBbox(bbox, page) {
  let [x1, y1, x2, y2] = bbox.map((v) => Math.round(v));
  x1 = Math.max(0, Math.min(page.width, x1));
  y1 = Math.max(0, Math.min(page.height, y1));
  x2 = Math.max(0, Math.min(page.width, x2));
  y2 = Math.max(0, Math.min(page.height, y2));
  if (x2 <= x1) x2 = Math.min(page.width, x1 + 1);
  if (y2 <= y1) y2 = Math.min(page.height, y1 + 1);
  return [x1, y1, x2, y2];
}

function overlayPoint(e) {
  const img = $("#preview-img");
  const page = currentPage();
  if (!img.complete || !img.naturalWidth || !page) return null;
  const rect = img.getBoundingClientRect();
  const px = Math.round(((e.clientX - rect.left) / rect.width) * page.width);
  const py = Math.round(((e.clientY - rect.top) / rect.height) * page.height);
  return { px, py, page, rect };
}

function blockAt(px, py) {
  const page = currentPage();
  if (!page) return -1;
  const blocks = page.blocks || [];
  for (let i = blocks.length - 1; i >= 0; i--) {
    const [x1, y1, x2, y2] = blocks[i].bbox;
    if (px >= x1 && px <= x2 && py >= y1 && py <= y2) return i;
  }
  return -1;
}

function overlayPointerDown(e) {
  const sel = state.sel;
  if (!sel) return;
  const p = overlayPoint(e);
  if (!p) return;
  e.preventDefault();
  const { px, py, page, rect } = p;
  const selected = selectedIndices();
  dragState = { pageIndex: sel.pageIndex, snapshot: cloneBlocks(page.blocks) };

  if (sel.drawMode) {
    // Drawing a brand-new block: rectangle from this point.
    dragState.mode = "draw";
    dragState.x1 = px; dragState.y1 = py;
    sel.pendingDraw = [px, py, px, py];
    return;
  }

  // Resize when the pointer is near a selected block's bottom-right handle.
  const HANDLE = 10; // device px around the handle
  for (const i of selected) {
    const b = page.blocks[i];
    if (!b) continue;
    const hx = (b.bbox[2] * rect.width) / page.width;
    const hy = (b.bbox[3] * rect.height) / page.height;
    if (Math.abs(e.clientX - rect.left - hx) <= HANDLE &&
        Math.abs(e.clientY - rect.top - hy) <= HANDLE) {
      dragState.mode = "resize";
      dragState.index = i;
      dragState.origin = [...b.bbox];
      $("#overlay-canvas").classList.add("grabbing");
      return;
    }
  }

  const bi = blockAt(px, py);
  if (bi >= 0) {
    if (!e.shiftKey && !e.ctrlKey && !e.metaKey && !isBlockSelected(bi)) {
      sel.selection = { pageIndex: sel.pageIndex, indices: new Set([bi]) };
      renderPage();
    } else if (e.shiftKey || e.ctrlKey || e.metaKey) {
      const was = isBlockSelected(bi);
      if (sel.selection.pageIndex !== sel.pageIndex) {
        sel.selection = { pageIndex: sel.pageIndex, indices: new Set() };
      }
      if (was) sel.selection.indices.delete(bi);
      else sel.selection.indices.add(bi);
      renderPage();
    }
    dragState.mode = "move";
    dragState.index = bi;
    dragState.startPx = px;
    dragState.startPy = py;
    dragState.origin = [...page.blocks[bi].bbox];
    $("#overlay-canvas").classList.add("grabbing");
  } else if (!e.shiftKey && !e.ctrlKey && !e.metaKey) {
    sel.selection = { pageIndex: sel.pageIndex, indices: new Set() };
    renderPage();
  }
}

function overlayPointerMove(e) {
  const sel = state.sel;
  if (!sel || !dragState) return;
  const p = overlayPoint(e);
  if (!p) return;
  e.preventDefault();
  const { px, py, page } = p;
  if (dragState.mode === "draw") {
    sel.pendingDraw = clampBbox(
      [Math.min(dragState.x1, px), Math.min(dragState.y1, py),
       Math.max(dragState.x1, px), Math.max(dragState.y1, py)], page);
    drawOverlay(page);
  } else if (dragState.mode === "move") {
    const b = page.blocks[dragState.index];
    if (!b) return;
    const dx = px - dragState.startPx;
    const dy = py - dragState.startPy;
    b.bbox = clampBbox([
      dragState.origin[0] + dx, dragState.origin[1] + dy,
      dragState.origin[2] + dx, dragState.origin[3] + dy,
    ], page);
    drawOverlay(page);
  } else if (dragState.mode === "resize") {
    const b = page.blocks[dragState.index];
    if (!b) return;
    b.bbox = clampBbox(
      [dragState.origin[0], dragState.origin[1], px, py], page);
    drawOverlay(page);
  }
}

function overlayPointerUp() {
  const sel = state.sel;
  const canvas = $("#overlay-canvas");
  if (canvas) canvas.classList.remove("grabbing");
  if (!sel || !dragState) return;
  const st = dragState;
  dragState = null;
  const page = currentPage();
  if (!page) return;
  const changed = (b, before) => b && b.bbox.join(",") !== before.join(",");
  if (st.mode === "draw") {
    const rect = sel.pendingDraw;
    if (rect && (rect[2] - rect[0]) >= 8 && (rect[3] - rect[1]) >= 8) {
      pushUndo();
      page.blocks.push({
        kind: "text", bbox: rect, text: "", caption: "",
        conf: null, font_scale: 1.0,
      });
      sel.selection = { pageIndex: sel.pageIndex, indices: new Set([page.blocks.length - 1]) };
      sel.drawMode = false;
      sel.embedded = false;
      setStatus("dirty", "running");
      toast(t("editor.blockAdded"), "success");
    }
    sel.pendingDraw = null;
    renderPage();
    return;
  }
  if ((st.mode === "move" || st.mode === "resize") && st.index != null) {
    const b = page.blocks[st.index];
    if (changed(b, st.origin)) {
      sel.undoStack.push({ pageIndex: st.pageIndex, blocks: st.snapshot });
      if (sel.undoStack.length > 50) sel.undoStack.shift();
      sel.embedded = false;
      setStatus("dirty", "running");
    }
    renderPage();
  }
}

/* ---------- embed (selected job) ---------- */
async function embed() {
  const sel = state.sel;
  if (!sel || !sel.pages.length) return;
  $("#btn-embed").disabled = true;
  $("#embed-status").textContent = t("embed.busy");
  // Output options: ocrmypdf's finalize stage applies optimization; the text
  // layer is rendered from the stored (possibly edited) hOCR pages.
  const optimize = $("#opt-optimize") ? $("#opt-optimize").value : "0";
  const outputType = $("#opt-output-type") ? $("#opt-output-type").value : "pdf";
  try {
    const out = await api(`/api/embed/${sel.jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: sel.jobId,
        optimize: parseInt(optimize, 10) || 0,
        output_type: outputType,
      }),
    });
    sel.embedded = true;
    setStatus("embedded", "done");
    const link = $("#download-link");
    link.classList.remove("hidden");
    link.href = out.url;
    link.textContent = t("embed.download", { name: out.filename });
    let extra = "";
    const imgs = out.images;
    if (imgs && imgs.optimize > 0) {
      extra = " " + t("embed.optStats", { n: imgs.optimize, bytes: imgs.output_type || "" });
    }
    $("#embed-status").textContent = t("embed.done") + extra;
    const job = jobById(sel.jobId);
    if (job) { job.has_embedded = true; renderJobs(); }
    const vBtn = $("#btn-validate");
    if (vBtn) vBtn.disabled = false;
    // The embed response carries the post-embed validation report (#17):
    // store it and show it right away when it was computed successfully.
    sel.lastReport = out.report || null;
    if (out.report && out.report.ok) {
      showReport(out.report);
    } else if (out.report && out.report.error) {
      hideReport();
      $("#report-status").textContent = t("report.error", { msg: out.report.error });
    }
    toast(t("toast.embedDone"), "success");
  } catch (e) {
    $("#embed-status").textContent = t("embed.failed", { msg: e.message });
    toast(t("embed.failed", { msg: e.message }), "error");
  } finally {
    $("#btn-embed").disabled = false;
  }
}

/* ---------- post-embed validation report (#17) ---------- */
function hideReport() {
  const panel = $("#report-panel");
  if (panel) panel.classList.add("hidden");
  const st = $("#report-status");
  if (st) st.textContent = "";
}

function _pct(v, fallback = null) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return fallback === null ? t("report.na") : fallback;
  return Math.round(Number(v) * 100) + "%";
}

function showReport(report) {
  const panel = $("#report-panel");
  const status = $("#report-status");
  if (!panel) return;
  if (!report || report.ok === false) {
    hideReport();
    if (status) status.textContent = t("report.error", { msg: (report && report.error) || "?" });
    return;
  }
  const summary = report.summary || {};
  const sumBox = $("#report-summary");
  if (sumBox) {
    sumBox.innerHTML = "";
    const avg = summary.avg_coverage !== undefined ? summary.avg_coverage : (summary.avg || 0);
    const thr = summary.threshold !== undefined ? summary.threshold : 0.6;
    const low = (summary.low_coverage_pages || []).length;
    const covLine = t("report.avgCoverage", { pct: _pct(avg) })
      + " · " + (low > 0
        ? t("report.lowPages", { n: low, pct: _pct(thr) })
        : t("report.allPass"));
    sumBox.appendChild(el("div", "report-line", covLine));
    const cb = summary.conf_buckets || {};
    sumBox.appendChild(el("div", "report-line",
      t("report.confBucket", { low: cb.low || 0, med: cb.medium || 0, high: cb.high || 0 })));
    sumBox.appendChild(el("div", "report-line", t("report.blocks", { n: summary.total_blocks || 0 })));
  }
  const wrap = $("#report-table-wrap");
  if (wrap) {
    wrap.innerHTML = "";
    const rows = report.pages || [];
    if (!rows.length) {
      wrap.appendChild(el("div", "hint", t("report.noPages")));
    } else {
      const table = document.createElement("table");
      table.className = "report-table";
      const thead = document.createElement("thead");
      const htr = document.createElement("tr");
      [t("report.page"), t("report.coverage"), t("report.colChars"),
       t("report.colWords"), t("report.conf"), t("report.flags")]
        .forEach((h) => {
          const th = document.createElement("th");
          th.textContent = h;
          htr.appendChild(th);
        });
      thead.appendChild(htr);
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      rows.forEach((r) => {
        const tr = document.createElement("tr");
        if (r.flags && r.flags.low_coverage) tr.classList.add("row-low");
        const td = (txt) => {
          const c = document.createElement("td");
          c.textContent = txt;
          return c;
        };
        tr.appendChild(td(String((r.page_index ?? 0) + 1)));
        tr.appendChild(td(r.flags && r.flags.empty_source ? t("report.na") : _pct(r.coverage)));
        tr.appendChild(td(r.embedded_chars + " / " + r.source_chars));
        tr.appendChild(td(r.embedded_words + " / " + r.source_words));
        const c = r.conf || {};
        const confTxt = c.count ? t("report.confLine", {
          avg: _pct(c.avg), min: _pct(c.min), max: _pct(c.max),
        }) : t("report.na");
        tr.appendChild(td(confTxt));
        const flags = [];
        if (r.flags) {
          if (r.flags.empty_source) flags.push(t("report.flagEmptySource"));
          if (r.flags.empty_embedded) flags.push(t("report.flagEmptyEmbedded"));
          if (r.flags.low_coverage) flags.push(t("report.flagLowCoverage"));
        }
        tr.appendChild(td(flags.join(", ") || t("report.ok")));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
    }
  }
  panel.classList.remove("hidden");
  if (status) status.textContent = "";
}

async function validateJob() {
  const sel = state.sel;
  const status = $("#report-status");
  if (!sel) return;
  const job = jobById(sel.jobId);
  if (status) status.textContent = t("report.busy");
  try {
    if (!job || !job.has_embedded) {
      hideReport();
      if (status) status.textContent = t("report.noEmbed");
      return;
    }
    const data = await api(`/api/validation/${sel.jobId}`);
    sel.lastReport = data;
    showReport(data);
  } catch (e) {
    hideReport();
    if (status) status.textContent = t("report.error", { msg: e.message });
  }
}

function downloadDataset() {
  const sel = state.sel;
  if (!sel || !sel.pages.length) return;
  const ds = {
    job_id: sel.jobId,
    generated_at: new Date().toISOString(),
    adapter_font_scale_def: t("dataset.def"),
    pages: sel.pages,
  };
  const blob = new Blob([JSON.stringify(ds, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `ocr_font_dataset_${sel.jobId}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

/* ---------- settings ---------- */
// OCRmyPDF pipeline knobs (persisted server-side; see backend/config.py).
const PIPELINE_IDS = {
  "ocr_engine": "set-ocr-engine",
  "ocrmypdf_mode": "set-ocrmypdf-mode",
  "ocrmypdf_language": "set-ocrmypdf-language",
};
const PIPELINE_CHECKBOX_IDS = {
  "ocrmypdf_deskew": "set-ocrmypdf-deskew",
  "ocrmypdf_clean": "set-ocrmypdf-clean",
  "generate_raw": "set-generate-raw",
  "export_reflow": "set-export-reflow",
  "export_llm": "set-export-llm",
};
// Free-form export knobs: [config key, element id, kind].  Numbers load as
// strings and are only sent back when non-empty (an empty field keeps the
// server default).
const EXPORT_INPUT_IDS = [
  ["export_llm_model", "set-export-llm-model", "text"],
  ["export_llm_threshold", "set-export-llm-threshold", "number"],
  ["export_llm_batch", "set-export-llm-batch", "number"],
  ["export_llm_timeout_s", "set-export-llm-timeout", "number"],
];

async function openSettings() {
  $("#settings-modal").classList.remove("hidden");
  $("#settings-status").textContent = "";
  try {
    const s = await api("/api/settings");
    $("#set-provider").value = s.provider || "ustc";
    $("#set-baseurl").value = s.base_url || "";
    $("#set-model").value = s.model || "";
    $("#set-apikey").value = s.has_api_key ? s.api_key_masked : "";
    Object.keys(PIPELINE_IDS).forEach((key) => {
      const elm = $(PIPELINE_IDS[key]);
      if (elm) elm.value = s[key] !== undefined && s[key] !== null ? String(s[key]) : "";
    });
    Object.keys(PIPELINE_CHECKBOX_IDS).forEach((key) => {
      const elm = $(PIPELINE_CHECKBOX_IDS[key]);
      if (elm) elm.checked = !!s[key];
    });
    EXPORT_INPUT_IDS.forEach(([key, id]) => {
      const elm = $(id);
      if (elm) elm.value = s[key] !== undefined && s[key] !== null ? String(s[key]) : "";
    });
  } catch (e) {
    $("#settings-status").textContent = t("settings.loadFailed", { msg: e.message });
  }
}

async function saveSettings() {
  const payload = {
    provider: $("#set-provider").value,
    base_url: $("#set-baseurl").value.trim(),
    model: $("#set-model").value.trim(),
    api_key: $("#set-apikey").value.trim(),
  };
  Object.keys(PIPELINE_IDS).forEach((key) => {
    const elm = $(PIPELINE_IDS[key]);
    if (elm && elm.value) payload[key] = elm.value;
  });
  Object.keys(PIPELINE_CHECKBOX_IDS).forEach((key) => {
    const elm = $(PIPELINE_CHECKBOX_IDS[key]);
    if (elm) payload[key] = elm.checked;
  });
  EXPORT_INPUT_IDS.forEach(([key, id]) => {
    const elm = $(id);
    if (elm && elm.value.trim()) payload[key] = elm.value.trim();
  });
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("#settings-status").textContent = t("settings.saved");
    toast(t("settings.saved"), "success");
    setTimeout(() => $("#settings-modal").classList.add("hidden"), 700);
  } catch (e) {
    $("#settings-status").textContent = t("settings.saveFailed", { msg: e.message });
  }
}

/* ---------- temp-file cleanup ---------- */
function fmtBytes(n) {
  if (!n || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + " " + units[i];
}

function areaSummary(name, a) {
  const line = el("div", "cleanup-line");
  line.appendChild(document.createTextNode(t("cleanup.area." + name) + ": "));
  line.appendChild(el("b", null, String(a.ready_count)));
  line.appendChild(document.createTextNode(
    ` ${t("cleanup.areaReady")} (${fmtBytes(a.ready_bytes)}), ${a.unreferenced_count} ${t("cleanup.areaUnref")} (${fmtBytes(a.unreferenced_bytes)})`));
  if (a.referenced_count) {
    line.appendChild(document.createTextNode(`, ${a.referenced_count} ${t("cleanup.areaInUse")}`));
  }
  return line;
}

async function openCleanup() {
  $("#cleanup-modal").classList.remove("hidden");
  const status = $("#cleanup-status");
  status.textContent = "";
  $("#cleanup-summary").innerHTML = "";
  $("#cleanup-summary").appendChild(el("div", "", t("cleanup.loading")));
  try {
    const data = await api("/api/cleanup");
    $("#cleanup-interval").textContent = String(Math.round(data.config.interval_hours));
    const age = data.config.max_age_hours;
    const box = $("#cleanup-max-age");
    box.value = String(Math.round(age));
    box.min = "1";

    const sum = $("#cleanup-summary");
    sum.innerHTML = "";
    const totals = data.totals || {};
    sum.appendChild(el("div", "cleanup-total",
      t("cleanup.total", { n: totals.ready_count, age: Math.round(age), bytes: fmtBytes(totals.ready_bytes) })));
    ["work", "output", "uploads"].forEach((area) => {
      if (data.areas && data.areas[area]) sum.appendChild(areaSummary(area, data.areas[area]));
    });
    if (!totals.ready_count) {
      sum.appendChild(el("div", "hint", t("cleanup.none")));
    }
  } catch (e) {
    status.textContent = t("cleanup.loadFailed", { msg: e.message });
  }
}

async function runCleanup(dryRun) {
  const btnRun = $("#btn-cleanup-run");
  const btnPrev = $("#btn-cleanup-preview");
  const status = $("#cleanup-status");
  const hours = parseFloat($("#cleanup-max-age").value) || 1;
  btnRun.disabled = true;
  btnPrev.disabled = true;
  status.textContent = dryRun ? t("cleanup.previewing") : t("cleanup.cleaning");
  try {
    const data = await api("/api/cleanup/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ older_than_hours: hours, dry_run: dryRun, force: false }),
    });
    const kept = data.kept || {};
    status.textContent = dryRun
      ? t("cleanup.previewDone", {
          n: data.deleted_count, bytes: fmtBytes(data.freed_bytes),
          inUse: kept.referenced || 0, fresh: kept.too_fresh || 0,
        })
      : t("cleanup.done", {
          n: data.deleted_count, bytes: fmtBytes(data.freed_bytes),
          inUse: kept.referenced || 0,
        });
    if (!dryRun) toast(t("cleanup.done", { n: data.deleted_count, bytes: fmtBytes(data.freed_bytes), inUse: kept.referenced || 0 }), "success");
    // Refresh the summary so the numbers reflect the cleanup just performed.
    openCleanup();
  } catch (e) {
    status.textContent = t("cleanup.failed", { msg: e.message });
  } finally {
    btnRun.disabled = false;
    btnPrev.disabled = false;
  }
}

/* ---------- debug logs ---------- */
async function refreshLogs() {
  try {
    const data = await api("/api/logs");
    const box = $("#log-box");
    box.textContent = (data.lines || []).join("\n");
    box.scrollTop = box.scrollHeight;
  } catch { /* ignore */ }
}

function toggleLogs() {
  const panel = $("#debug-panel");
  const hidden = panel.classList.contains("hidden");
  panel.classList.toggle("hidden");
  if (hidden) {
    refreshLogs();
    if ($("#auto-log").checked) startLogPolling();
  } else {
    stopLogPolling();
  }
}

function startLogPolling() {
  stopLogPolling();
  state.logTimer = setInterval(refreshLogs, 2000);
}

function stopLogPolling() {
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
}

/* ---------- wire up ---------- */
function prevPage() {
  const s = state.sel;
  if (s && s.pageIndex > 0) { s.pageIndex--; renderTabs(); renderPage(); }
}

function nextPage() {
  const s = state.sel;
  if (s && s.pageIndex < s.pages.length - 1) { s.pageIndex++; renderTabs(); renderPage(); }
}

/* Re-render dynamic parts after a locale switch (static markup is handled by
   I18N.applyDocument()). */
function onLocaleChanged() {
  updateAdapterUI();
  updatePageRangeHint();
  updateZipButton();   // the job ZIP button label is locale-dependent (count)
  setGlobalStatus();
  renderJobs();
  renderPendingPanel();   // per-file rows are built from i18n labels
  if (state.sel) {
    const job = jobById(state.sel.jobId);
    $("#editing-job").textContent = job ? t("workspace.editing", { name: job.filename }) : "";
    if (state.sel.pages.length) renderPage();
  }
}

async function init() {
  // --- locale: saved preference > browser language ---
  const browserLocale = (navigator.language || "en").toLowerCase().indexOf("zh") === 0 ? "zh" : "en";
  const savedLocale = getPref(PREFS.locale, null);
  I18N.setLocale(savedLocale || browserLocale, false);
  const langSel = $("#lang-select");
  if (langSel) langSel.value = I18N.locale;

  // --- theme: saved preference > auto ---
  state.themePref = getPref(PREFS.theme, "auto");
  applyTheme(state.themePref);
  const themeSel = $("#theme-select");
  if (themeSel) themeSel.value = state.themePref;

  // --- restore other UI preferences ---
  const savedAdapter = getPref(PREFS.adapter, null);
  if (savedAdapter) $("#adapter").value = savedAdapter;
  const savedLang = getPref(PREFS.tessLang, "");
  if (savedLang) $("#tess-lang").value = savedLang;
  const savedConc = getPref(PREFS.concurrency, null);
  if (savedConc) $("#concurrency").value = savedConc;
  const savedZoom = parseFloat(getPref(PREFS.zoom, ""));
  if (savedZoom >= 50 && savedZoom <= 200) {
    state.zoom = savedZoom;
    $("#zoom").value = String(savedZoom);
  }

  // --- confidence review preferences ---
  state.confFilter = getPref(PREFS.confFilter, "") === "1";
  const confBox = $("#conf-filter");
  if (confBox) confBox.checked = state.confFilter;
  const savedThr = parseFloat(getPref(PREFS.confThreshold, "60"));
  if (savedThr >= 1 && savedThr <= 100) {
    state.confThreshold = savedThr;
    const thrBox = $("#conf-threshold");
    if (thrBox) thrBox.value = String(Math.round(savedThr));
  }

  // --- output option preferences (optimize / output type) ---
  const savedOpt = getPref(PREFS.optimize, "0");
  if (["0", "1", "2", "3"].includes(savedOpt)) {
    const optBox = $("#opt-optimize");
    if (optBox) optBox.value = savedOpt;
  }
  const savedOutType = getPref(PREFS.outputType, "pdf");
  if (["pdf", "pdfa"].includes(savedOutType)) {
    const otBox = $("#opt-output-type");
    if (otBox) otBox.value = savedOutType;
  }

  // 有任务运行时关闭标签页 → 浏览器原生关闭确认提示（任意一个任务在跑都会提示）。
  window.addEventListener("beforeunload", (e) => {
    if (!anyRunning()) return;
    e.preventDefault();
    e.returnValue = "";
  });

  // dropzone（可随时上传，支持多任务并行）
  const drop = $("#drop-zone");
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (e) => handleFiles(e.dataTransfer.files));
  $("#file-input").addEventListener("change", (e) => handleFiles(e.target.files));
  $("#btn-zip").onclick = downloadZip;
  $("#btn-start-jobs").onclick = startPendingJobs;
  $("#btn-discard-files").onclick = discardPending;

  $("#btn-prev").onclick = prevPage;
  $("#btn-next").onclick = nextPage;
  $("#btn-reocr").onclick = reOcrPage;
  // Re-validate the page-range inputs as the user types (disable on bad ranges).
  ["#page-start", "#page-end"].forEach((sel) => {
    const input = $(sel);
    if (input) input.addEventListener("input", updatePageRangeHint);
  });
  $("#zoom").oninput = (e) => {
    state.zoom = parseFloat(e.target.value);
    $("#zoom-label").textContent = state.zoom + "%";
    setPref(PREFS.zoom, e.target.value);
    renderPage();
  };
  $("#btn-embed").onclick = embed;
  // --- post-embed validation report (#17) ---
  const vBtn = $("#btn-validate");
  if (vBtn) vBtn.onclick = validateJob;
  const rBtn = $("#btn-revalidate");
  if (rBtn) rBtn.onclick = validateJob;
  const cBtn = $("#btn-report-close");
  if (cBtn) cBtn.onclick = hideReport;

  // --- interactive overlay for block operations (#6) ---
  const ocv = $("#overlay-canvas");
  if (ocv) {
    ocv.addEventListener("pointerdown", overlayPointerDown);
    ocv.addEventListener("pointermove", overlayPointerMove);
    ocv.addEventListener("pointerup", overlayPointerUp);
    ocv.addEventListener("pointercancel", overlayPointerUp);
    ocv.addEventListener("pointerleave", overlayPointerUp);
  }
  $("#btn-settings").onclick = openSettings;
  $("#btn-cleanup").onclick = openCleanup;

  // --- confidence review controls ---
  $("#conf-filter").onchange = () => {
    state.confFilter = $("#conf-filter").checked;
    setPref(PREFS.confFilter, state.confFilter ? "1" : "0");
    renderPage();
  };
  $("#conf-threshold").onchange = () => {
    const v = Math.max(1, Math.min(100, parseFloat($("#conf-threshold").value) || 60));
    state.confThreshold = v;
    $("#conf-threshold").value = String(Math.round(v));
    setPref(PREFS.confThreshold, String(Math.round(v)));
    renderTabs();
    renderPage();
  };

  // --- output option controls (persist only; read at embed time) ---
  $("#opt-optimize").onchange = () => setPref(PREFS.optimize, $("#opt-optimize").value || "0");
  $("#opt-output-type").onchange = () =>
    setPref(PREFS.outputType, $("#opt-output-type").value || "pdf");
  $("#btn-cleanup-preview").onclick = () => runCleanup(true);
  $("#btn-cleanup-run").onclick = () => runCleanup(false);
  $("#btn-cleanup-cancel").onclick = () => $("#cleanup-modal").classList.add("hidden");
  $("#cleanup-modal").onclick = (e) => { if (e.target === $("#cleanup-modal")) $("#cleanup-modal").classList.add("hidden"); };
  $("#btn-logs").onclick = toggleLogs;
  $("#btn-refresh-logs").onclick = refreshLogs;
  $("#auto-log").onchange = (e) => { if (e.target.checked) startLogPolling(); else stopLogPolling(); };
  $("#btn-settings-save").onclick = saveSettings;
  $("#btn-settings-cancel").onclick = () => $("#settings-modal").classList.add("hidden");
  $("#settings-modal").onclick = (e) => { if (e.target === $("#settings-modal")) $("#settings-modal").classList.add("hidden"); };
  $("#adapter").onchange = updateAdapterUI;

  // --- language / theme switchers ---
  langSel.addEventListener("change", (e) => I18N.setLocale(e.target.value));
  themeSel.addEventListener("change", (e) => {
    state.themePref = e.target.value;
    setPref(PREFS.theme, state.themePref);
    applyTheme(state.themePref);
  });
  MEDIA_DARK.addEventListener("change", () => applyTheme(state.themePref));

  // --- persist per-control preferences ---
  $("#adapter").addEventListener("change", () => setPref(PREFS.adapter, $("#adapter").value));
  $("#tess-lang").addEventListener("change", () => setPref(PREFS.tessLang, $("#tess-lang").value.trim()));
  $("#concurrency").addEventListener("change", () => setPref(PREFS.concurrency, $("#concurrency").value));

  // --- keyboard shortcuts: Ctrl/Cmd+Enter = embed; ←/→ = page navigation ---
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!document.querySelector(".modal:not(.hidden)")) embed();
      return;
    }
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    // (#6) A focused block editor owns the arrow keys (bbox nudge).
    if (e.target.closest && e.target.closest(".block")) return;
    if (e.key === "ArrowLeft") prevPage();
    else if (e.key === "ArrowRight") nextPage();
  });

  // Re-render dynamic UI when the language changes.
  document.addEventListener("i18n:changed", onLocaleChanged);

  updateAdapterUI();
  updatePageRangeHint();
  updateZipButton();
  renderPendingPanel();

  let health = null;
  try { health = await api("/api/health"); setStatus("online"); }
  catch { setStatus("offline", "error"); }
  initQuitButton(health);

  // The server knows all jobs — show every one of them (running ones get SSE).
  loadJobs();
}

// --- desktop mode: offer the only way out in the browser-fallback mode -----
// (In windowed mode closing the window quits; in browser mode this button is
// what stops the local server.)
function initQuitButton(health) {
  const btn = $("#btn-quit");
  if (!btn) return;
  if (!health || !health.desktop) { btn.classList.add("hidden"); return; }
  btn.classList.remove("hidden");
  btn.onclick = async () => {
    if (!window.confirm(I18N.t("header.quitConfirm"))) return;
    btn.disabled = true;
    try {
      // The custom header is required by the endpoint (CSRF guard).
      await api("/api/app/quit", {
        method: "POST",
        headers: { "X-PDF-OCR-Embed": "quit" },
      });
      setStatus("offline");
    } catch (e) {
      btn.disabled = false;
      window.alert(I18N.t("header.quitFailed"));
    }
  };
}

init();