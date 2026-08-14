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
};

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
    state.pages = [];
    state.pageIndex = 0;
    state.embedded = false;
    state.running = true;
    setStatus("running", "running");
    $("#stop-btn").classList.remove("hidden");
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
}

function showStopped(message) {
  if (state.es) { state.es.close(); state.es = null; }
  _stopRunning();
  setStatus("stopped", "error");
  const done = state.pages.length;
  $("#progress-label").textContent = `${message} (${done} page(s) completed)`;
  $("#retry-btn").classList.remove("hidden");
  $("#partial-btn").classList.toggle("hidden", done === 0);
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

  wrapper.appendChild(meta);
  wrapper.appendChild(textarea);
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
      body: JSON.stringify({ job_id: state.jobId, pages: state.pages }),
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
async function init() {
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
  $("#retry-btn").onclick = retryOcr;
  $("#stop-btn").onclick = stopOcr;
  $("#partial-btn").onclick = downloadPartial;
  $("#btn-settings").onclick = openSettings;
  $("#btn-logs").onclick = toggleLogs;
  $("#btn-refresh-logs").onclick = refreshLogs;
  $("#auto-log").onchange = (e) => { if (e.target.checked) startLogPolling(); else stopLogPolling(); };
  $("#btn-settings-save").onclick = saveSettings;
  $("#btn-settings-cancel").onclick = () => $("#settings-modal").classList.add("hidden");
  $("#settings-modal").onclick = (e) => { if (e.target === $("#settings-modal")) $("#settings-modal").classList.add("hidden"); };
  $("#adapter").onchange = updateAdapterUI;
  updateAdapterUI();

  try { await api("/api/health"); setStatus("online"); }
  catch { setStatus("offline", "error"); }
}

init();