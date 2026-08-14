/* PDF OCR Embed single-page WebUI — multi-job.

   The backend is the single source of truth for every OCR task: it holds them
   all in memory, so this page simply asks "what jobs are there?" on load and
   subscribes to live progress (SSE) per running job. No client-side job-id
   persistence, no localStorage. Jobs survive closing the tab; several may run
   in parallel and each is managed from its own card.
*/
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const RUNNING_STATUSES = new Set(["uploaded", "running", "retrying"]);
const STATUS_LABEL = {
  uploaded: "starting",
  running: "running",
  retrying: "retrying",
  stopped: "stopped",
  done: "done",
  error: "error",
  embedded: "embedded",
};

const state = {
  jobs: [],     // job summaries: {id, filename, status, current, total, error, has_embedded, created, busy}
  sel: null,    // editor session for the selected job: {jobId, pages, pageIndex, embedded}
  zoom: 100,
  es: {},       // jobId -> EventSource
  logTimer: null,
  embedFont: "",   // selected system font name for the text layer
};

/* ---------- helpers ---------- */
function setStatus(text, cls) {
  const elx = $("#conn-status");
  elx.textContent = text;
  elx.className = "pill" + (cls ? " " + cls : "");
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
    hint.textContent = "Local OCR — no API key needed. Configure Tesseract language (e.g. chi_sim, eng, chi_sim+eng).";
  } else {
    langRow.classList.add("hidden");
    hint.textContent = adapter === "unlimited"
      ? "Higher = faster on many pages, but more concurrent API calls."
      : "Generic OpenAI vision model — set API key / base URL / model in Settings.";
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
    if (msg) msg.textContent = "Please choose a PDF file.";
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
  } catch (e) {
    setStatus("error", "error");
    if (msg) msg.textContent = "Upload failed: " + e.message;
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
                       STATUS_LABEL[job.status] || job.status));
  head.appendChild(title);
  head.appendChild(el("span", "job-count", `${job.current} / ${job.total || "?"}`));
  card.appendChild(head);

  const bar = el("div", "bar");
  const fill = el("div", "fill");
  fill.style.width = job.total ? Math.round((job.current / job.total) * 100) + "%" : "2%";
  bar.appendChild(fill);
  card.appendChild(bar);

  if (job.error) card.appendChild(el("div", "job-err", "✗ " + job.error));

  const actions = el("div", "job-actions");
  const active = RUNNING_STATUSES.has(job.status);

  if (active) {
    const stop = el("button", "warn", job.busy ? "Stopping…" : "Stop");
    stop.disabled = !!job.busy;
    stop.onclick = () => stopJob(job.id);
    actions.appendChild(stop);
  } else {
    if (job.status === "error" || job.current > 0) {
      const retry = el("button", "primary", job.current > 0 ? "Retry remaining" : "Retry");
      retry.disabled = !!job.busy;
      retry.onclick = () => retryJob(job.id);
      actions.appendChild(retry);
    }
    if (job.current > 0) {
      const partial = el("button", "primary", "Download partial");
      partial.disabled = !!job.busy;
      partial.onclick = () => partialJob(job.id);
      actions.appendChild(partial);
    }
    const clear = el("button", "warn", "Clear");
    clear.onclick = () => clearJob(job.id);
    actions.appendChild(clear);
  }

  if (job.current > 0) {
    const edit = el("button", "small", "Edit pages");
    edit.onclick = () => selectJob(job.id);
    actions.appendChild(edit);
  }
  if (job.has_embedded) {
    const a = el("a", "download-link", "⬇ Embedded PDF");
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
    Object.assign(job, {
      status: RUNNING_STATUSES.has(msg.status) ? msg.status : job.status,
      current: msg.current,
      total: msg.total,
      error: null,
    });
  } else if (msg.type === "status") {
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
    Object.assign(job, { status: "error", error: "Stop failed: " + e.message });
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
    Object.assign(job, { status: "error", error: "Retry failed: " + e.message });
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
    if (!pages.length) throw new Error("No completed pages");
    const out = await api(`/api/embed/${jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, pages }),
    });
    window.open(out.url, "_blank");
  } catch (e) {
    Object.assign(job, { error: "Partial failed: " + e.message });
  } finally {
    job.busy = false;
    renderJobs();
  }
}

async function clearJob(jobId) {
  const job = jobById(jobId);
  if (!job) return;
  if (!confirm(`Delete job "${job.filename}" entirely?\n\nThis removes its OCR results, working files and any embedded PDF.`)) {
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
  if (label) label.textContent = job ? `Editing: ${job.filename}` : "";
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
        el("div", "embed-hint", "No pages OCR'd yet — they appear here as each page finishes."));
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
  sel.pages.forEach((_, i) => {
    const tab = el("button", "page-tab" + (i === sel.pageIndex ? " active" : ""), String(i + 1));
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
    blocksBox.appendChild(el("div", "", "No text blocks on this page."));
  }
  (page.blocks || []).forEach((block, bi) => {
    blocksBox.appendChild(buildBlockEditor(block, bi));
  });

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
    ctx.strokeStyle = block.kind === "image" ? "#7a5cff" : "#2f6fed";
    ctx.lineWidth = 2;
    ctx.strokeRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
  });
}

function buildBlockEditor(block, bi) {
  const wrapper = el("div", "block");
  wrapper.dataset.bi = bi;
  const meta = el("div", "meta");
  meta.appendChild(el("span", "badge", block.kind));
  meta.appendChild(el("div", "coords", block.bbox.join(", ") + " px"));

  const textarea = document.createElement("textarea");
  textarea.value = block.text || block.caption || "";
  textarea.placeholder = "Text…";
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
  del.title = "Remove block";
  del.onclick = () => {
    const sel = state.sel;
    if (!sel) return;
    sel.pages[sel.pageIndex].blocks.splice(bi, 1);
    renderPage();
  };

  // ---- Interactive font-size control (debug: too big / too small) ----
  if (block.font_scale == null) block.font_scale = 1.0;
  const fsRow = el("div", "fs-row");
  const fsLabel = el("span", "fs-label", "font size");
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
  const fsReset = el("button", "small", "auto");
  fsReset.title = "Reset to auto (1.0)";
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
  $("#embed-status").textContent = "Embedding…";
  try {
    const out = await api(`/api/embed/${sel.jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: sel.jobId, pages: sel.pages, embed_font: state.embedFont }),
    });
    sel.embedded = true;
    setStatus("embedded", "done");
    const link = $("#download-link");
    link.classList.remove("hidden");
    link.href = out.url;
    link.textContent = "Download " + out.filename;
    $("#embed-status").textContent = "Embedded. Text is now selectable/searchable.";
    const job = jobById(sel.jobId);
    if (job) { job.has_embedded = true; renderJobs(); }
  } catch (e) {
    $("#embed-status").textContent = "Embed failed: " + e.message;
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
    $("#embed-status").textContent = "Preview failed: " + e.message;
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
      if (row) row.textContent = `${fi.derived_fs}pt → ${fi.fs}pt (${fi.lines} ln)`;
    });
  } catch (e) { /* ignore */ }
}

function downloadDataset() {
  const sel = state.sel;
  if (!sel || !sel.pages.length) return;
  const ds = {
    job_id: sel.jobId,
    generated_at: new Date().toISOString(),
    adapter_font_scale_def: "font_scale multiplies the auto font size",
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
    $("#settings-status").textContent = "Failed to load settings: " + e.message;
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
    $("#settings-status").textContent = "Saved.";
    setTimeout(() => $("#settings-modal").classList.add("hidden"), 700);
  } catch (e) {
    $("#settings-status").textContent = "Save failed: " + e.message;
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
    sel.innerHTML = '<option value="">Auto (default)</option>';
    fonts.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.name;
      opt.textContent = f.name + (f.family ? " — " + f.family : "");
      sel.appendChild(opt);
    });
    if (state.embedFont) sel.value = state.embedFont;
  } catch (e) { /* fonts unavailable; keep Auto */ }
}

async function init() {
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

  $("#btn-prev").onclick = () => { const s = state.sel; if (s && s.pageIndex > 0) { s.pageIndex--; renderTabs(); renderPage(); } };
  $("#btn-next").onclick = () => { const s = state.sel; if (s && s.pageIndex < s.pages.length - 1) { s.pageIndex++; renderTabs(); renderPage(); } };
  $("#zoom").oninput = (e) => { state.zoom = parseFloat(e.target.value); renderPage(); };
  $("#btn-embed").onclick = embed;
  $("#btn-preview").onclick = previewOverlay;
  $("#btn-dataset").onclick = downloadDataset;
  $("#btn-settings").onclick = openSettings;
  $("#btn-logs").onclick = toggleLogs;
  $("#btn-refresh-logs").onclick = refreshLogs;
  $("#auto-log").onchange = (e) => { if (e.target.checked) startLogPolling(); else stopLogPolling(); };
  $("#btn-settings-save").onclick = saveSettings;
  $("#btn-settings-cancel").onclick = () => $("#settings-modal").classList.add("hidden");
  $("#settings-modal").onclick = (e) => { if (e.target === $("#settings-modal")) $("#settings-modal").classList.add("hidden"); };
  $("#adapter").onchange = updateAdapterUI;
  $("#embed-font").onchange = (e) => {
    state.embedFont = e.target.value;
    if (state.sel) state.sel.embedded = false;
    setStatus("dirty", "running");
  };
  updateAdapterUI();
  loadFonts();

  try { await api("/api/health"); setStatus("online"); }
  catch { setStatus("offline", "error"); }

  // The server knows all jobs — show every one of them (running ones get SSE).
  loadJobs();
}

init();