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
  embedFont: "pdfocr.ui.embedFont",
  confFilter: "pdfocr.ui.confFilter",
  confThreshold: "pdfocr.ui.confThreshold",
  imgMode: "pdfocr.ui.imgMode",
  imgQuality: "pdfocr.ui.imgQuality",
  imgDownscale: "pdfocr.ui.imgDownscale",
  linearize: "pdfocr.ui.linearize",
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
  zoom: 100,
  es: {},       // jobId -> EventSource
  logTimer: null,
  embedFont: "",   // selected system font name for the text layer
  confFilter: false,   // show only low-confidence blocks in the editor
  confThreshold: 60,   // 1..100 — blocks below are flagged low-confidence
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
      : t("upload.hint.generic");
  }
}

function currentAdapterCfg() {
  const adapter = $("#adapter").value;
  const cfg = { adapter };
  if (adapter === "tesseract") {
    const lang = $("#tess-lang").value.trim();
    if (lang) cfg.lang = lang;
  }
  // API adapters use the provider keys from Settings; engine-specific models
  // fall back to whatever is configured server-side.
  return cfg;
}

async function handleFile(file) {
  const msg = $("#upload-msg");
  if (msg) msg.textContent = "";
  if (!file || !file.name.toLowerCase().endsWith(".pdf")) {
    setStatus("error", "error");
    if (msg) msg.textContent = t("upload.notPdf");
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  const cfg = currentAdapterCfg();
  fd.append("adapter", cfg.adapter);
  if (cfg.lang) fd.append("lang", cfg.lang);
  const concurrency = Math.max(1, Math.min(32, parseInt($("#concurrency").value || "1", 10)));
  fd.append("concurrency", String(concurrency));

  setStatus("uploading", "running");
  try {
    const data = await api("/api/ocr/upload", { method: "POST", body: fd });
    state.jobs.unshift({
      id: data.job_id,
      filename: data.filename || file.name,
      status: "running",
      current: 0,
      total: 0,
      error: null,
      has_embedded: false,
      created: Date.now() / 1000,
      busy: false,
    });
    renderJobs();
    connectStream(data.job_id);
    selectJob(data.job_id);
    setStatus("running", "running");
    if (msg) msg.textContent = "";
    toast(t("upload.started"), "success");
  } catch (e) {
    setStatus("error", "error");
    if (msg) msg.textContent = t("upload.failed", { msg: e.message });
    toast(t("upload.failed", { msg: e.message }), "error");
  }
}

/* ---------- jobs: list + per-job SSE ---------- */
async function loadJobs() {
  try {
    const data = await api("/api/jobs");
    state.jobs = (data.jobs || []).map((j) => Object.assign(j, { busy: false }));
    state.jobs.sort((a, b) => (b.created || 0) - (a.created || 0));
    // Drop streams for jobs that no longer exist server-side.
    Object.keys(state.es).forEach((id) => {
      if (!jobById(id)) { state.es[id].close(); delete state.es[id]; }
    });
    renderJobs();
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

  if (job.error) card.appendChild(el("div", "job-err", "✗ " + job.error));

  const actions = el("div", "job-actions");
  const active = RUNNING_STATUSES.has(job.status);

  if (active) {
    const stop = el("button", "warn", job.busy ? t("job.stopping") : t("job.stop"));
    stop.disabled = !!job.busy;
    stop.onclick = () => stopJob(job.id);
    actions.appendChild(stop);
  } else {
    if (job.status === "error" || job.current > 0) {
      const retry = el("button", "primary", job.current > 0 ? t("job.retryRemaining") : t("job.retry"));
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
    const a = el("a", "download-link", t("job.embeddedPdf"));
    a.href = `/api/download/${job.id}.pdf`;
    a.download = "";
    actions.appendChild(a);
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
      Object.assign(job, {
        status: RUNNING_STATUSES.has(msg.status) ? msg.status : job.status,
        current: msg.current,
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
    state.es[jobId] = null;
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
  const cfg = currentAdapterCfg();
  fd.append("adapter", cfg.adapter);
  if (cfg.lang) fd.append("lang", cfg.lang);
  const c = Math.max(1, Math.min(32, parseInt($("#concurrency").value || "1", 10)));
  fd.append("concurrency", String(c));
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
  if (state.sel && state.sel.jobId === jobId) setSelectedJob(null);
  renderJobs();
  setGlobalStatus();
}

/* ---------- selected-job editor ---------- */
async function selectJob(jobId) {
  state.sel = { jobId, pages: [], pageIndex: 0, embedded: false };
  const job = jobById(jobId);
  const label = $("#editing-job");
  if (label) label.textContent = job ? t("workspace.editing", { name: job.filename }) : "";
  $("#download-link").classList.add("hidden");
  $("#embed-status").textContent = "";
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
    const job = jobById(sel.jobId);
    if (job) Object.assign(job, { current: pages.length, total: data.total || job.total });
    $("#btn-embed").disabled = !pages.length;
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
    const tab = el("button", "page-tab" + (i === sel.pageIndex ? " active" : ""), String(i + 1));
    const low = pageLowConfCount(pg);
    if (low) {
      tab.appendChild(el("span", "tab-badge", String(low)));
      tab.title = t("editor.confPageBadge", { n: low, p: state.confThreshold });
    }
    tab.onclick = () => { sel.pageIndex = i; renderTabs(); renderPage(); };
    wrap.appendChild(tab);
  });
  $("#btn-prev").disabled = sel.pageIndex === 0;
  $("#btn-next").disabled = sel.pageIndex >= sel.pages.length - 1;
}

function renderPage() {
  const sel = state.sel;
  if (!sel) return;
  const page = sel.pages[sel.pageIndex];
  if (!page) return;

  const img = $("#preview-img");
  img.src = `/api/pages/${sel.jobId}/${page.page_index}/image?ts=${Date.now()}`;
  img.onload = () => drawOverlay(page);
  img.onerror = () => {};

  state.sourceW = page.width;
  state.sourceH = page.height;

  // Editor blocks
  const blocksBox = $("#blocks");
  blocksBox.innerHTML = "";
  if (!page.blocks || page.blocks.length === 0) {
    blocksBox.appendChild(el("div", "", t("editor.noBlocks")));
  }
  (page.blocks || []).forEach((block, bi) => {
    if (state.confFilter && !isLowConf(block)) return;
    blocksBox.appendChild(buildBlockEditor(block, bi));
  });
  if (state.confFilter && blocksBox.childElementCount === 0
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
  loadFontInfo();  // annotate each block with its derived / applied font size
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

  (page.blocks || []).forEach((block) => {
    const [x1, y1, x2, y2] = block.bbox;
    let color = block.kind === "image" ? "#7a5cff" : "#2f6fed";
    if (block.kind !== "image") {
      const cls = confClass(confPct(block));
      if (cls === "conf-low") color = "#e85d3a";
      else if (cls === "conf-med") color = "#f0a020";
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
  });
}

function buildBlockEditor(block, bi) {
  const wrapper = el("div", "block");
  wrapper.dataset.bi = bi;
  const pct = confPct(block);
  if (confClass(pct) === "conf-low") wrapper.classList.add("block-low");
  const meta = el("div", "meta");
  meta.appendChild(el("span", "badge", block.kind));
  meta.appendChild(el("span", "conf-badge " + confClass(pct),
    pct === null ? t("editor.confNa") : Math.round(pct) + "%"));
  meta.appendChild(el("div", "coords", block.bbox.join(", ") + " px"));

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

  const del = el("button", "del", "✕");
  del.title = t("editor.removeBlock");
  del.onclick = () => {
    const sel = state.sel;
    if (!sel) return;
    sel.pages[sel.pageIndex].blocks.splice(bi, 1);
    renderPage();
  };

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
  wrapper.appendChild(fsRow);
  wrapper.appendChild(del);
  return wrapper;
}

/* ---------- embed (selected job) ---------- */
async function embed() {
  const sel = state.sel;
  if (!sel || !sel.pages.length) return;
  $("#btn-embed").disabled = true;
  $("#embed-status").textContent = t("embed.busy");
  const imgMode = $("#img-mode") ? $("#img-mode").value : "none";
  const imgQuality = $("#img-quality") ? parseInt($("#img-quality").value || "75", 10) : 75;
  const imgDownscaleRaw = $("#img-downscale") ? $("#img-downscale").value : "";
  const imgDownscale = imgDownscaleRaw ? parseInt(imgDownscaleRaw, 10) : null;
  const linearize = !!( $("#opt-linearize") && $("#opt-linearize").checked);
  try {
    const out = await api(`/api/embed/${sel.jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: sel.jobId, pages: sel.pages, embed_font: state.embedFont,
        img_mode: imgMode, img_quality: imgQuality,
        img_downscale: imgDownscale, linearize: linearize,
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
    if (imgs && imgs.replaced > 0) {
      extra = " " + t("embed.optStats", { n: imgs.replaced, bytes: fmtBytes(imgs.saved_bytes) });
    }
    if (imgs && imgs.linearized === true) extra += " " + t("embed.linearized");
    else if (linearize && imgs && imgs.linearized === false) extra += " " + t("embed.linearUnavailable");
    $("#embed-status").textContent = t("embed.done") + extra;
    const job = jobById(sel.jobId);
    if (job) { job.has_embedded = true; renderJobs(); }
    toast(t("toast.embedDone"), "success");
  } catch (e) {
    $("#embed-status").textContent = t("embed.failed", { msg: e.message });
    toast(t("embed.failed", { msg: e.message }), "error");
  } finally {
    $("#btn-embed").disabled = false;
  }
}

/* ---------- interactive font-size debug ---------- */
async function previewOverlay() {
  const sel = state.sel;
  if (!sel) return;
  const page = sel.pages[sel.pageIndex];
  if (!page) return;
  $("#preview-img").classList.add("loading");
  try {
    // POST current pages (with font_scale) so the overlay reflects the sliders.
    const resp = await fetch(`/api/preview/${sel.jobId}/${page.page_index}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pages: sel.pages, embed_font: state.embedFont }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const img = $("#preview-img");
    img.onload = () => { img.classList.remove("loading"); };
    img.src = url;
    setStatus("preview", "running");
  } catch (e) {
    setStatus("error", "error");
    $("#embed-status").textContent = t("embed.previewFailed", { msg: e.message });
  }
}

async function loadFontInfo() {
  const sel = state.sel;
  if (!sel) return;
  const page = sel.pages[sel.pageIndex];
  if (!page) return;
  try {
    const data = await api(`/api/fontinfo/${sel.jobId}/${page.page_index}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pages: sel.pages, embed_font: state.embedFont }),
    });
    // Refresh derived-fs annotations in the editor blocks.
    (data.blocks || []).forEach((fi) => {
      const row = document.querySelector(`.block[data-bi="${fi.index}"] .fs-der`);
      if (row) row.textContent = t("fontInfo.line", { derived: fi.derived_fs, fs: fi.fs, lines: fi.lines });
    });
  } catch (e) { /* ignore */ }
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
async function openSettings() {
  $("#settings-modal").classList.remove("hidden");
  $("#settings-status").textContent = "";
  try {
    const s = await api("/api/settings");
    $("#set-provider").value = s.provider || "ustc";
    $("#set-baseurl").value = s.base_url || "";
    $("#set-model").value = s.model || "";
    $("#set-apikey").value = s.has_api_key ? s.api_key_masked : "";
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
  refreshCacheInfo();  // OCR result-cache panel in the same modal
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

/* ---------- OCR result cache ---------- */
async function refreshCacheInfo() {
  const sum = $("#cache-summary");
  const status = $("#cache-status");
  if (!sum) return;
  try {
    const data = await api("/api/cache");
    sum.innerHTML = "";
    sum.appendChild(el("div", "",
      t("cache.entries", { n: data.entries, bytes: fmtBytes(data.bytes) })
      + " · " + t("cache.hitsMisses", { hits: data.hits, misses: data.misses })));
    sum.appendChild(el("div", "hint", data.enabled
      ? t("cache.ttlHours", { h: Math.round(data.max_age_hours) })
      : t("cache.disabled")));
    if (status) status.textContent = "";
  } catch (e) {
    if (sum) {
      sum.innerHTML = "";
      sum.appendChild(el("div", "hint", t("cache.loadFailed", { msg: e.message })));
    }
  }
}

async function clearOcrCache() {
  const btn = $("#btn-cache-clear");
  const status = $("#cache-status");
  if (!btn) return;
  btn.disabled = true;
  if (status) status.textContent = t("cache.clearing");
  try {
    const data = await api("/api/cache/clear", { method: "POST" });
    const msg = t("cache.cleared", { n: data.removed, bytes: fmtBytes(data.freed_bytes) });
    if (status) status.textContent = msg;
    toast(msg, "success");
    await refreshCacheInfo();
  } catch (e) {
    if (status) status.textContent = t("cache.failed", { msg: e.message });
  } finally {
    btn.disabled = false;
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
async function loadFonts() {
  const sel = $("#embed-font");
  if (!sel) return;
  try {
    const data = await api("/api/fonts");
    const fonts = (data.fonts || []);
    sel.innerHTML = '<option value=""></option>';
    sel.options[0].textContent = t("workspace.autoFont");
    fonts.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.name;
      opt.textContent = f.name + (f.family ? " — " + f.family : "");
      sel.appendChild(opt);
    });
    if (state.embedFont) sel.value = state.embedFont;
  } catch (e) { /* fonts unavailable; keep Auto */ }
}

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
  setGlobalStatus();
  renderJobs();
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
  state.embedFont = getPref(PREFS.embedFont, "");

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

  // --- output optimization preferences ---
  const savedImgMode = getPref(PREFS.imgMode, "none");
  if (["none", "jpeg", "gray-jpeg"].includes(savedImgMode)) {
    const imBox = $("#img-mode");
    if (imBox) imBox.value = savedImgMode;
  }
  const savedQ = parseFloat(getPref(PREFS.imgQuality, "75"));
  if (savedQ >= 20 && savedQ <= 100) {
    const qBox = $("#img-quality");
    if (qBox) qBox.value = String(Math.round(savedQ));
  }
  const savedDs = getPref(PREFS.imgDownscale, "");
  if (["2", "4"].includes(savedDs)) {
    const dsBox = $("#img-downscale");
    if (dsBox) dsBox.value = savedDs;
  }
  const linBox = $("#opt-linearize");
  if (linBox) linBox.checked = getPref(PREFS.linearize, "") === "1";

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
  drop.addEventListener("drop", (e) => handleFile(e.dataTransfer.files[0]));
  $("#file-input").addEventListener("change", (e) => handleFile(e.target.files[0]));

  $("#btn-prev").onclick = prevPage;
  $("#btn-next").onclick = nextPage;
  $("#zoom").oninput = (e) => {
    state.zoom = parseFloat(e.target.value);
    $("#zoom-label").textContent = state.zoom + "%";
    setPref(PREFS.zoom, e.target.value);
    renderPage();
  };
  $("#btn-embed").onclick = embed;
  $("#btn-preview").onclick = previewOverlay;
  $("#btn-dataset").onclick = downloadDataset;
  $("#btn-settings").onclick = openSettings;
  $("#btn-cleanup").onclick = openCleanup;
  $("#btn-cache-clear").onclick = clearOcrCache;

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

  // --- output optimization controls (persist only; read at embed time) ---
  $("#img-mode").onchange = () => setPref(PREFS.imgMode, $("#img-mode").value || "none");
  $("#img-quality").onchange = () => setPref(PREFS.imgQuality, $("#img-quality").value || "75");
  $("#img-downscale").onchange = () => setPref(PREFS.imgDownscale, $("#img-downscale").value);
  $("#opt-linearize").onchange = () =>
    setPref(PREFS.linearize, $("#opt-linearize").checked ? "1" : "0");
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
  $("#embed-font").addEventListener("change", (e) => {
    state.embedFont = e.target.value;
    setPref(PREFS.embedFont, state.embedFont);
    if (state.sel) state.sel.embedded = false;
    setStatus("dirty", "running");
  });

  // --- keyboard shortcuts: Ctrl/Cmd+Enter = embed; ←/→ = page navigation ---
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!document.querySelector(".modal:not(.hidden)")) embed();
      return;
    }
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === "ArrowLeft") prevPage();
    else if (e.key === "ArrowRight") nextPage();
  });

  // Re-render dynamic UI when the language changes.
  document.addEventListener("i18n:changed", onLocaleChanged);

  updateAdapterUI();
  loadFonts();

  try { await api("/api/health"); setStatus("online"); }
  catch { setStatus("offline", "error"); }

  // The server knows all jobs — show every one of them (running ones get SSE).
  loadJobs();
}

init();