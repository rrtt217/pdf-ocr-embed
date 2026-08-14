/* PDF OCR Embed single-page WebUI */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  jobId: null,
  pages: [],            // normalized page dicts (only completed pages)
  pageIndex: 0,
  running: false,
  embedded: false,
  sourceW: 0,
  sourceH: 0,
  zoom: 100,
  es: null,             // active EventSource
  logTimer: null,
  embedFont: "",        // selected system font name for the text layer
};

/* Last job id is persisted so a tab reload restores the in-progress task
   (jobs live server-side; only the id needs remembering). */
const LS_JOB_KEY = "pdfocr.jobId.v1";
function saveJobId() {
  try {
    if (state.jobId) localStorage.setItem(LS_JOB_KEY, state.jobId);
    else localStorage.removeItem(LS_JOB_KEY);
  } catch { /* storage unavailable — resumption simply won't work */ }
}

/* ---------- helpers ---------- */
function setStatus(text, cls) {
  const el = $("#conn-status");
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(body || ("HTTP " + res.status));
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

/* ---------- upload ---------- */
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
  if (state.running) return;
  if (!file || !file.name.toLowerCase().endsWith(".pdf")) {
    alert("Please choose a PDF file.");
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  const cfg = currentAdapterCfg();
  fd.append("adapter", cfg.adapter);
  if (cfg.lang) fd.append("lang", cfg.lang);
  const concurrencyInput = $("#concurrency");
  const concurrency = Math.max(1, Math.min(32, parseInt(concurrencyInput.value || "1", 10)));
  concurrencyInput.value = concurrency;
  fd.append("concurrency", String(concurrency));

  setStatus("uploading", "running");
  $("#upload-zone").classList.add("hidden");
  $("#progress-section").classList.remove("hidden");
  $("#workspace").classList.add("hidden");
  $("#retry-btn").classList.add("hidden");
  $("#stop-btn").classList.add("hidden");
  $("#partial-btn").classList.add("hidden");
  $("#progress-fill").style.width = "2%";
  $("#progress-label").textContent = "Uploading…";

  try {
    const data = await api("/api/ocr/upload", { method: "POST", body: fd });
    state.jobId = data.job_id;
    saveJobId();
    state.pages = [];
    state.pageIndex = 0;
    state.embedded = false;
    state.running = true;
    setStatus("running", "running");
    $("#stop-btn").classList.remove("hidden");
    $("#retry-btn").classList.add("hidden");
    $("#partial-btn").classList.add("hidden");
    $("#clear-btn").classList.add("hidden");
    $("#progress-label").textContent =
      `OCR running (parallel: ${data.concurrency || 1}) …`;
    connectStream(state.jobId);
  } catch (e) {
    setStatus("error", "error");
    $("#progress-label").textContent = "Upload failed: " + e.message;
  }
}

function connectStream(jobId) {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/ocr/stream/${jobId}`);
  state.es = es;
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "progress") {
      $("#progress-fill").style.width = (msg.current / msg.total) * 100 + "%";
      $("#progress-count").textContent = `${msg.current} / ${msg.total}`;
      $("#progress-label").textContent = "OCR " + msg.message + "…";
    } else if (msg.type === "warning") {
      $("#progress-label").textContent = "Warning: " + msg.message;
    } else if (msg.type === "error_page") {
      $("#progress-label").textContent = `Page ${msg.page_index + 1} failed: ` + msg.message;
    } else if (msg.type === "status" && msg.status === "done") {
      $("#progress-fill").style.width = "100%";
      $("#progress-label").textContent = "OCR complete";
      if (msg.result) state.pages = msg.result.filter(p => p);
      _stopRunning();
      finishOcr();
      es.close();
      state.es = null;
    } else if (msg.type === "status" && msg.status === "stopped") {
      if (msg.result) state.pages = msg.result.filter(p => p);
      _stopRunning();
      showStopped(msg.message || "OCR stopped");
    } else if (msg.type === "error") {
      showOcrError(msg.message);
    }
  };
  es.onerror = () => { showOcrError("Connection to server lost"); };
}

function _stopRunning() {
  state.running = false;
  $("#stop-btn").classList.add("hidden");
}

function showOcrError(message) {
  if (state.es) { state.es.close(); state.es = null; }
  _stopRunning();
  setStatus("error", "error");
  $("#progress-label").textContent = "OCR error: " + message;
  $("#retry-btn").classList.remove("hidden");
  $("#partial-btn").classList.toggle("hidden", state.pages.length === 0);
  $("#clear-btn").classList.remove("hidden");
}

function showStopped(message) {
  if (state.es) { state.es.close(); state.es = null; }
  _stopRunning();
  setStatus("stopped", "error");
  const done = state.pages.length;
  $("#progress-label").textContent = `${message} (${done} page(s) completed)`;
  $("#retry-btn").classList.remove("hidden");
  $("#partial-btn").classList.toggle("hidden", done === 0);
  $("#clear-btn").classList.remove("hidden");
}

/* ---------- job restore / clear ---------- */

async function restoreJob() {
  const jobId = localStorage.getItem(LS_JOB_KEY);
  if (!jobId) return;
  let data;
  try {
    data = await api(`/api/pages/${jobId}`);
  } catch (e) {
    // Job no longer exists server-side (e.g. server restarted) — drop stale id.
    localStorage.removeItem(LS_JOB_KEY);
    return;
  }
  state.jobId = jobId;
  state.pages = (data.pages || []).filter(p => p);
  const st = data.status;

  if (st === "running" || st === "retrying" || st === "uploaded") {
    // Still active — resume live progress over SSE.
    state.running = true;
    state.embedded = false;
    setStatus(st, "running");
    $("#progress-section").classList.remove("hidden");
    $("#stop-btn").classList.remove("hidden");
    $("#retry-btn").classList.add("hidden");
    $("#partial-btn").classList.add("hidden");
    $("#clear-btn").classList.add("hidden");
    const total = data.total || state.pages.length;
    const cur = state.pages.length;
    $("#progress-fill").style.width = total ? (cur / total) * 100 + "%" : "2%";
    $("#progress-count").textContent = `${cur} / ${total}`;
    $("#progress-label").textContent =
      `Restored job (${cur}/${total} pages done) — resuming…`;
    connectStream(jobId);
  } else if (st === "done" || st === "embedded") {
    await finishOcr();  // hides progress, shows workspace with the completed pages
    if (st === "embedded" && data.has_embedded) {
      const link = $("#download-link");
      link.classList.remove("hidden");
      link.href = `/api/download/${jobId}.pdf`;
      link.textContent = "Download embedded PDF";
      state.embedded = true;
      setStatus("embedded", "done");
    }
  } else if (st === "stopped") {
    showStopped("OCR stopped earlier (recovered from last session)");
  } else if (st === "error") {
    showOcrError("OCR failed earlier");
  }
}

async function clearJob() {
  if (!state.jobId) return;
  if (!confirm("Delete this job entirely?\n\nThis removes the OCR results, "
               + "the uploaded PDF's working files and any embedded PDF.")) {
    return;
  }
  try {
    await api(`/api/ocr/clear/${state.jobId}`, { method: "POST" });
  } catch (e) { /* job may already be gone — proceed to reset the UI */ }
  if (state.es) { state.es.close(); state.es = null; }
  state.jobId = null;
  state.pages = [];
  state.pageIndex = 0;
  state.running = false;
  state.embedded = false;
  saveJobId();  // removes the stored id
  setStatus("idle", "");
  $("#progress-section").classList.add("hidden");
  $("#workspace").classList.add("hidden");
  $("#stop-btn").classList.add("hidden");
  $("#retry-btn").classList.add("hidden");
  $("#partial-btn").classList.add("hidden");
  $("#clear-btn").classList.add("hidden");
  $("#download-link").classList.add("hidden");
  $("#btn-embed").disabled = true;
  const drop = $("#drop-zone");
  if (drop) drop.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function stopOcr() {
  if (!state.jobId || !state.running) return;
  $("#stop-btn").disabled = true;
  $("#progress-label").textContent = "Stopping…";
  try {
    await api(`/api/ocr/stop/${state.jobId}`, { method: "POST" });
    // Fetch completed pages immediately from the server — don't wait for SSE.
    const data = await api(`/api/pages/${state.jobId}`);
    if (data.pages) state.pages = data.pages.filter(p => p);
    _stopRunning();
    setStatus("stopped", "error");
    const done = state.pages.length;
    $("#progress-label").textContent = `Stopped (${done} page(s) completed)`;
    $("#retry-btn").classList.remove("hidden");
    $("#partial-btn").classList.toggle("hidden", done === 0);
    $("#clear-btn").classList.remove("hidden");
    $("#stop-btn").disabled = false;
  } catch (e) {
    $("#stop-btn").disabled = false;
    $("#progress-label").textContent = "Stop failed: " + e.message;
  }
}

async function retryOcr() {
  if (!state.jobId || state.running) return;
  setStatus("retrying", "running");
  $("#retry-btn").classList.add("hidden");
  $("#partial-btn").classList.add("hidden");
  $("#clear-btn").classList.add("hidden");
  $("#stop-btn").classList.remove("hidden");
  $("#stop-btn").disabled = false;
  $("#progress-label").textContent = "Retrying (only missing pages)…";
  const concurrencyInput = $("#concurrency");
  const concurrency = Math.max(1, Math.min(32, parseInt(concurrencyInput.value || "1", 10)));
  const fd = new FormData();
  const cfg = currentAdapterCfg();
  fd.append("adapter", cfg.adapter);
  if (cfg.lang) fd.append("lang", cfg.lang);
  fd.append("concurrency", String(concurrency));
  try {
    const data = await api(`/api/ocr/retry/${state.jobId}`, { method: "POST", body: fd });
    state.running = true;
    state.embedded = false;
    // Keep state.pages (done pages preserved); backend only re-runs missing ones.
    $("#progress-label").textContent =
      `Retrying (parallel: ${data.concurrency || 1}) …`;
    connectStream(state.jobId);
  } catch (e) {
    _stopRunning();
    setStatus("error", "error");
    $("#progress-label").textContent = "Retry failed: " + e.message;
    $("#retry-btn").classList.remove("hidden");
  }
}

async function downloadPartial() {
  if (!state.jobId || !state.pages.length) return;
  $("#partial-btn").disabled = true;
  $("#progress-label").textContent = "Embedding partial result…";
  try {
    const out = await api(`/api/embed/${state.jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: state.jobId, pages: state.pages }),
    });
    $("#progress-label").textContent =
      `Partial PDF ready (${state.pages.length} page(s) embedded).`;
    window.open(out.url, "_blank");
  } catch (e) {
    $("#progress-label").textContent = "Partial embed failed: " + e.message;
  } finally {
    $("#partial-btn").disabled = false;
  }
}

async function finishOcr() {
  setStatus("done", "done");
  $("#btn-embed").disabled = false;
  try {
    const data = await api(`/api/pages/${state.jobId}`);
    if (data.pages && data.pages.length) state.pages = data.pages.filter(p => p);
  } catch (e) { /* pages may already be in state from SSE */ }
  $("#progress-section").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  state.pageIndex = 0;
  renderTabs();
  renderPage();
  $("#btn-embed").disabled = state.pages.length === 0;
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

/* ---------- page tabs ---------- */
function renderTabs() {
  const wrap = $("#page-tabs");
  wrap.innerHTML = "";
  state.pages.forEach((_, i) => {
    const tab = el("button", "page-tab" + (i === state.pageIndex ? " active" : ""), (i + 1));
    tab.onclick = () => { state.pageIndex = i; renderTabs(); renderPage(); };
    wrap.appendChild(tab);
  });
  $("#btn-prev").disabled = state.pageIndex === 0;
  $("#btn-next").disabled = state.pageIndex >= state.pages.length - 1;
}

/* ---------- page render ---------- */
function renderPage() {
  const page = state.pages[state.pageIndex];
  if (!page) return;

  const img = $("#preview-img");
  const zoomed = state.pages.length === 1 ? false : true;
  img.src = `/api/pages/${state.jobId}/${page.page_index}/image?ts=${Date.now()}`;
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
    const page = state.pages[state.pageIndex];
    const target = page.blocks[bi];
    if (target) target.text = e.target.value;
    state.embedded = false;
    setStatus("dirty", "running");
  };

  const del = el("button", "del", "✕");
  del.title = "Remove block";
  del.onclick = () => {
    state.pages[state.pageIndex].blocks.splice(bi, 1);
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
    state.embedded = false;
    setStatus("dirty", "running");
  };
  // Reset to auto
  const fsReset = el("button", "small", "auto");
  fsReset.title = "Reset to auto (1.0)";
  fsReset.onclick = () => {
    block.font_scale = 1.0;
    fsSlider.value = "1.0";
    fsVal.textContent = "1.00×";
    state.embedded = false; setStatus("dirty", "running");
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

/* ---------- embed ---------- */
async function embed() {
  if (!state.jobId || !state.pages.length) return;
  $("#btn-embed").disabled = true;
  $("#embed-status").textContent = "Embedding…";
  try {
    const out = await api(`/api/embed/${state.jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: state.jobId, pages: state.pages, embed_font: state.embedFont }),
    });
    state.embedded = true;
    setStatus("embedded", "done");
    const link = $("#download-link");
    link.classList.remove("hidden");
    link.href = out.url;
    link.textContent = "Download " + out.filename;
    $("#embed-status").textContent = "Embedded. Text is now selectable/searchable.";
  } catch (e) {
    $("#embed-status").textContent = "Embed failed: " + e.message;
  } finally {
    $("#btn-embed").disabled = false;
  }
}

/* ---------- interactive font-size debug ---------- */
async function previewOverlay() {
  const page = state.pages[state.pageIndex];
  if (!page || !state.jobId) return;
  $("#preview-img").classList.add("loading");
  try {
    // POST current pages (with font_scale) so the overlay reflects the sliders.
    const resp = await fetch(`/api/preview/${state.jobId}/${page.page_index}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pages: state.pages, embed_font: state.embedFont }),
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
  const page = state.pages[state.pageIndex];
  if (!page || !state.jobId) return;
  try {
    const data = await api(`/api/fontinfo/${state.jobId}/${page.page_index}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pages: state.pages, embed_font: state.embedFont }),
    });
    // Refresh derived-fs annotations in the editor blocks.
    (data.blocks || []).forEach((fi) => {
      const row = document.querySelector(`.block[data-bi="${fi.index}"] .fs-der`);
      if (row) row.textContent = `${fi.derived_fs}pt → ${fi.fs}pt (${fi.lines} ln)`;
    });
  } catch (e) { /* ignore */ }
}

function downloadDataset() {
  if (!state.pages.length) return;
  const ds = {
    job_id: state.jobId,
    generated_at: new Date().toISOString(),
    adapter_font_scale_def: "font_scale multiplies the auto font size",
    pages: state.pages,
  };
  const blob = new Blob([JSON.stringify(ds, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stem = (state.jobId || "job");
  a.href = url;
  a.download = `ocr_font_dataset_${stem}.json`;
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
  // 任务（OCR / 重试）进行中关闭标签页 → 浏览器原生关闭确认提示。
  // 使用原生 beforeunload，不引入自定义文案（新版浏览器会忽略 returnValue 文本，
  // 显示各自固定的提示）。state.running 为 false 时不拦截。
  window.addEventListener("beforeunload", (e) => {
    if (!state.running) return;
    e.preventDefault();
    e.returnValue = "";
  });

  // dropzone
  const drop = $("#drop-zone");
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (e) => handleFile(e.dataTransfer.files[0]));
  $("#file-input").addEventListener("change", (e) => handleFile(e.target.files[0]));

  $("#btn-prev").onclick = () => { if (state.pageIndex > 0) { state.pageIndex--; renderTabs(); renderPage(); } };
  $("#btn-next").onclick = () => { if (state.pageIndex < state.pages.length - 1) { state.pageIndex++; renderTabs(); renderPage(); } };
  $("#zoom").oninput = (e) => { state.zoom = parseFloat(e.target.value); renderPage(); };
  $("#btn-embed").onclick = embed;
  $("#btn-preview").onclick = previewOverlay;
  $("#btn-dataset").onclick = downloadDataset;
  $("#retry-btn").onclick = retryOcr;
  $("#stop-btn").onclick = stopOcr;
  $("#partial-btn").onclick = downloadPartial;
  $("#clear-btn").onclick = clearJob;
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
    state.embedded = false;
    setStatus("dirty", "running");
  };
  updateAdapterUI();
  loadFonts();

  try { await api("/api/health"); setStatus("online"); }
  catch { setStatus("offline", "error"); }

  // Restore the previous session's OCR task, if any (resumes running jobs too).
  restoreJob();
}

init();