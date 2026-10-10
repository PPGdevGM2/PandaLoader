/* PandaLoader web UI. Vanilla JS, polls /api/state every 2 s. */
"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let selected = null;          // scene shown in the details panel (last clicked)
let selection = new Set();    // multi-selection for batch actions
let anchor = null;            // row index for shift-click ranges
let logTab = "events";
let pollTimer = null;

// ---------- helpers ----------
function hms(s) {
  if (s == null || isNaN(s)) return "--:--";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtDate(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtTime(t) {
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function base(p) { return String(p || "").split(/[\\/]/).pop(); }
function dirname(p) { const s = String(p || ""); const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\")); return i > 0 ? s.slice(0, i) : ""; }
function toast(msg, ok = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (ok ? " ok" : ""); t.hidden = false;
  clearTimeout(t._timer); t._timer = setTimeout(() => (t.hidden = true), ok ? 2500 : 6000);
}
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || r.statusText);
  return data;
}
async function act(path, body = {}, okMsg) {
  try { const d = await api(path, body); if (okMsg) toast(okMsg, true); await refresh(); return d; }
  catch (e) { toast(e.message); }
}

// ---------- rendering ----------
let lastPollMs = Date.now();
async function refresh() {
  try {
    state = await api("/api/state");
    lastPollMs = Date.now();
    render();
  } catch (e) {
    $("summary").textContent = "connection lost";
  }
}

function render() {
  const s = state, q = s.queue;
  if (!$("settingsDlg").hidden) renderDenoiserState();
  maybeShowSetup();
  // header
  $("version").textContent = s.version ? `v${s.version}` : "";
  $("queueName").textContent = q.path ? base(q.path).replace(/\.rq\.json$/, "") : "no queue open";
  $("queuePath").textContent = q.path || "";
  const etaTxt = s.running ? ` · ETA <b>${hms(q.eta)}</b>` : (s.finished ? " · finished" : "");
  $("summary").innerHTML = q.total ? `<b>${q.done}</b>/${q.total} done${q.failed ? ` · <b class="err">${q.failed} failed</b>` : ""}${etaTxt}` : "";
  $("btnRender").hidden = s.running;
  $("btnPause").hidden = !s.running || s.paused;
  $("btnResume").hidden = !s.running || !s.paused;
  $("btnStop").hidden = !s.running;
  $("btnRender").disabled = !q.path || (q.pending === 0 && !(q.toDenoise > 0));
  $("btnRender").title = q.pending === 0 && q.toDenoise > 0 ? `Nothing to render; ${q.toDenoise} finished scene(s) wait for denoising` : "Start rendering pending scenes";
  $("shutdownAfter").checked = !!s.shutdownAfterQueue;
  $("counts").textContent = q.total ? `${q.pending} pending · ${q.running} running · ${q.done} done · ${q.failed} failed` + (q.toDenoise ? ` · ${q.toDenoise} to denoise` : "") + (q.averageSecs ? ` · avg ${hms(q.averageSecs)}` : "") : "";

  // pending PC shutdown
  const banner = $("shutdownBanner");
  if (s.shutdownAt) {
    banner.hidden = false;
    $("shutdownCountdown").textContent = Math.max(0, Math.round(s.shutdownAt - s.now));
    document.title = "⚠ SHUTDOWN · PandaLoader";
  } else if (!banner.hidden) { banner.hidden = true; document.title = "PandaLoader"; }

  // workers
  $("workers").innerHTML = s.workers.map(workerCard).join("");

  // table (left alone while a row is being dragged, or the drag would be cut off)
  const find = $("find").value.trim().toLowerCase();
  const tb = $("scenes").querySelector("tbody");
  if (!drag.active) tb.innerHTML = s.scenes.map((sc) => sceneRow(sc, find)).join("");
  $("emptyHint").hidden = s.scenes.length > 0;
  if (selected != null && !s.scenes[selected]) { selected = null; $("details").hidden = true; }
  if (selected != null) renderDetails(s.scenes[selected], false);
  selection = new Set([...selection].filter((i) => i < s.scenes.length));
  renderBatchBar();

  // log
  renderLog();
}

// ---------- GPU gauges ----------
// ---------- resizable scene list ----------
const TABLE_H_KEY = "tableHeight";
function applyTableHeight(h) {
  const col = document.querySelector(".table-col"), wrap = document.querySelector(".table-wrap"), det = $("details");
  col.classList.toggle("sized", !!h);
  wrap.style.height = h ? `${h}px` : "";
  det.style.maxHeight = h ? `${h + 50}px` : "";   // batch bar + gap above the table: bottoms stay aligned
}
(function initTableResize() {
  let saved = null;
  try { saved = Number(localStorage.getItem(TABLE_H_KEY)) || null; } catch (e) {}
  applyTableHeight(saved);
  const grip = $("tableResize"), wrap = document.querySelector(".table-wrap");
  let h = saved;
  grip.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); grip.classList.add("active");
    try { grip.setPointerCapture(e.pointerId); } catch (e2) {}
    const y0 = e.clientY, h0 = wrap.getBoundingClientRect().height;
    const move = (ev) => { h = Math.max(100, Math.round(h0 + ev.clientY - y0)); applyTableHeight(h); };
    const up = () => {
      window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); window.removeEventListener("pointercancel", up);
      grip.classList.remove("active");
      try { localStorage.setItem(TABLE_H_KEY, String(h)); } catch (e2) {}
    };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); window.addEventListener("pointercancel", up);
  });
  grip.addEventListener("dblclick", () => { h = null; applyTableHeight(null); try { localStorage.removeItem(TABLE_H_KEY); } catch (e) {} });
})();

function gaugesHidden(name) { try { return localStorage.getItem("gauges:" + name) === "hidden"; } catch (e) { return false; } }
function setGaugesHidden(name, hidden) { try { localStorage.setItem("gauges:" + name, hidden ? "hidden" : "shown"); } catch (e) {} }
function donut(frac, label, value, cls) {
  const r = 15.9155, pct = Math.max(0, Math.min(1, frac || 0)) * 100;
  return `<div class="gauge ${cls}"><svg viewBox="0 0 36 36"><circle class="track" cx="18" cy="18" r="${r}"/><circle class="fill" cx="18" cy="18" r="${r}" stroke-dasharray="${pct.toFixed(1)} ${(100 - pct).toFixed(1)}"/><text x="18" y="20.5">${esc(value)}</text></svg><span class="glabel">${esc(label)}</span></div>`;
}
function workerGpus(w) {
  const all = state.gpus || [];
  if (w.gpu == null || w.gpu === "") return all;
  const idx = String(w.gpu).split(",").map((x) => Number(x));
  return all.filter((g) => idx.includes(g.index));
}
function gaugesHtml(w) {
  const gpus = workerGpus(w);
  if (!gpus.length) return "";
  return `<div class="gauges">${gpus.map((g) => {
    const used = g.memUsed == null ? 0 : g.memUsed / 1024, total = g.memTotal ? g.memTotal / 1024 : 0;
    const tip = [g.name, g.temp != null ? `${g.temp} °C` : "", g.d3d != null ? `3D ${Math.round(g.d3d)}%` : "", g.util != null ? `total ${Math.round(g.util)}%` : ""].filter(Boolean).join(" · ");
    const cuda = g.cuda != null ? g.cuda : null;  // Cuda engine (Task Manager's "Cuda"), not the chip-wide load
    return `<div class="gpu-gauges" title="${esc(tip)}"><span class="gtitle">GPU ${g.index}</span>`
      + donut(total ? used / total : 0, "VRAM", total ? `${used.toFixed(1)}G` : "n/a", "vram")
      + donut((cuda || 0) / 100, "CUDA", cuda == null ? "n/a" : `${Math.round(cuda)}%`, "cuda") + `</div>`;
  }).join("")}</div>`;
}
function gaugeToggle(w) {
  const hidden = gaugesHidden(w.name);
  return `<button class="gtoggle" data-worker="${esc(w.name)}" title="${hidden ? "Show" : "Hide"} GPU gauges">${hidden ? "◔" : "◕"}</button>`;
}

function workerCard(w) {
  const st = w.state || "idle";
  const gauges = gaugesHidden(w.name) ? "" : gaugesHtml(w);
  let frac = 0;
  if (st === "starting") frac = 0.03;
  else if (st === "loading") frac = 0.1;
  else if (st === "rendering") {
    if (w.frames) {
      const per = w.expectedSecs / w.frames;
      frac = 0.1 + 0.88 * Math.min(1, (w.framesDone + Math.min(1, per ? w.renderElapsed / per : 0)) / w.frames);
    } else frac = 0.1 + 0.88 * Math.min(1, w.expectedSecs ? w.renderElapsed / w.expectedSecs : 0);
  }
  else if (st === "saving" || st === "finished") frac = 1;
  const gpu = w.gpu == null ? "all GPUs" : `GPU ${w.gpu}`;
  if (st === "idle") {
    return `<div class="worker idle"><div class="head"><span class="wname">${esc(w.name)}</span><span class="gpu">${gpu}</span><span class="phase idle">idle</span>${gaugeToggle(w)}</div>
      <div class="body"><div class="main">
      <div class="scene">&nbsp;</div><div class="bar"><i></i></div>
      <div class="stats"><span>done <b>${w.jobsDone}</b></span><span>failed <b>${w.jobsFailed}</b></span>${w.lastError ? `<span class="err">${esc(w.lastError)}</span>` : ""}</div>
      </div>${gauges}</div></div>`;
  }
  return `<div class="worker ${st}">
    <div class="head"><span class="wname">${esc(w.name)}</span><span class="gpu">${gpu}</span><span class="phase ${st}">${st}</span>${gaugeToggle(w)}</div>
    <div class="body"><div class="main">
    <div class="scene">${esc(w.sceneName)}${w.frames ? ` <span class="hint">frame ${w.frame} · ${w.framesDone}/${w.frames} of this block</span>` : ""}<span class="cam">${esc(w.camera || "")}</span></div>
    <div class="bar"><i style="width:${(frac * 100).toFixed(1)}%"></i></div>
    <div class="stats">
      <span>iterations <b>${Math.round(w.iterations)}</b></span>
      <span>render <b>${hms(w.renderElapsed)}</b> / ~${hms(w.expectedSecs)}</span>
      <span>job <b>${hms(w.jobElapsed)}</b></span>
      <span>ETA <b>${hms(w.eta)}</b></span>
      <span class="spacer"></span><span>done ${w.jobsDone}${w.jobsFailed ? ` · failed ${w.jobsFailed}` : ""}</span>
    </div>
    ${w.lastError ? `<div class="err">${esc(w.lastError)}</div>` : ""}
    </div>${gauges}</div>
  </div>`;
}
$("workers").addEventListener("click", (ev) => {
  const b = ev.target.closest("button.gtoggle"); if (!b) return;
  setGaugesHidden(b.dataset.worker, !gaugesHidden(b.dataset.worker)); render();
});

function sceneRow(sc, find) {
  const found = find && (sc.name.toLowerCase().includes(find) || (sc.camera || "").toLowerCase().includes(find));
  const inSel = selection.has(sc.index);
  const cls = [sc.index === selected ? "selected" : "", inSel ? "multi" : "", found ? "found" : ""].join(" ");
  const stateTxt = sc.stateName === "running" && sc.worker ? `${sc.worker}` : sc.stateName;
  const cam = sc.savedCamera ? (sc.camera ? `${esc(sc.camera)} <span class="hint">(saved)</span>` : `<span class="hint">saved with scene</span>`) : `<b>${esc(sc.camera)}</b>`;
  const assets = [sc.postLoaders.length ? `${sc.postLoaders.length} PRA` : "", sc.imageSize ? `${sc.imageSize[0]}×${sc.imageSize[1]}` : ""].filter(Boolean).join(" · ");
  let last = sc.lastSecs ? hms(sc.lastSecs) : "";
  if (sc.anim) {
    const frac = sc.framesTotal ? sc.framesDone / sc.framesTotal : 0;
    last = `<span class="frames-cell" title="animation: frames ${sc.frameFirst}-${sc.frameLast}${sc.lastSecs ? `, ${hms(sc.lastSecs)} per frame` : ""}"><span class="fbar"><i style="width:${(frac * 100).toFixed(0)}%"></i></span>${sc.framesDone}/${sc.framesTotal} frames</span>`;
  }
  const err = sc.lastError ? ` title="${esc(sc.lastError)}"` : "";
  const dnMark = sc.denoised === true ? `<span class="dnok" title="denoised">✓</span>` : sc.denoised === false ? `<span class="dnfail" title="denoise failed, see queue log">!</span>`
    : (sc.denoise && sc.stateName === "done") ? `<span class="dnwait" title="waiting: will be denoised by the next run">…</span>` : "";
  const dn = `<input type="checkbox" class="dnbox" data-act="denoise" ${sc.denoise ? "checked" : ""} title="Post-render denoise${state.config.denoiserValid ? "" : " (no valid denoiser path in Settings: flag is skipped)"}">${dnMark}`;
  return `<tr class="${cls}" data-i="${sc.index}"${err} draggable="true">
    <td class="sel"><input type="checkbox" class="selbox" ${inSel ? "checked" : ""}></td>
    <td class="num">${sc.index + 1}</td>
    <td class="state"><span class="badge ${sc.stateName}">${esc(stateTxt)}</span></td>
    <td>${esc(sc.name)}.duf</td>
    <td class="hint">${fmtDate(sc.mtime)}</td>
    <td>${cam}</td>
    <td>${last}</td>
    <td class="hint">${assets}</td>
    <td class="dn">${dn}</td>
    <td class="target mono" title="${esc(sc.target)}">${esc(sc.target)}</td>
    <td class="actions">
      <button data-act="move" data-d="-1" title="Move up">↑</button>
      <button data-act="move" data-d="1" title="Move down">↓</button>
      ${sc.stateName === "pending" ? `<button data-act="done" title="Skip: mark as done">✓</button>` : `<button data-act="reset" title="Reset to pending">↺</button>`}
      <button data-act="remove" title="Remove from queue">✕</button>
    </td></tr>`;
}

function renderDetails(sc, full) {
  const d = $("details");
  d.hidden = false;
  $("dTitle").textContent = sc.name;
  $("dFile").textContent = sc.file;
  $("dState").innerHTML = `<span class="badge ${sc.stateName}">${sc.stateName}</span>`
    + (sc.anim ? ` animation ${sc.framesDone}/${sc.framesTotal} frames` : "") + (sc.lastSecs ? ` · last render ${hms(sc.lastSecs)}${sc.anim ? "/frame" : ""}` : "");
  $("dError").textContent = sc.lastError || "";
  const img = $("dThumb");
  if (sc.targetExists) {
    const src = `/api/scene/${sc.index}/thumb?w=800`;
    if (img.dataset.src !== src) { img.src = src; img.dataset.src = src; }
    img.hidden = false; $("dNoThumb").hidden = true; $("dThumbLink").href = `/api/scene/${sc.index}/image`;
  } else { img.hidden = true; img.dataset.src = ""; $("dNoThumb").hidden = false; $("dThumbLink").removeAttribute("href"); }
  $("dDenoisedLink").hidden = !sc.denoisedFile;
  if (sc.denoisedFile) $("dDenoisedA").href = `/api/scene/${sc.index}/denoised`;
  if (full || d.dataset.index !== String(sc.index)) {
    d.dataset.index = sc.index;
    $("dTarget").value = sc.target || "";
    $("dAssets").value = sc.postLoaders.join("\n");
    $("dDenoise").checked = !!sc.denoise;
    $("dDenoiseHint").textContent = state.config.denoiserValid ? (sc.denoised === true ? "last render was denoised" : sc.denoised === false ? "last denoise failed, see queue log" : "") : "no valid denoiser path in Settings";
    $("dSizeOn").checked = !!sc.imageSize; $("dSizeRow").hidden = !sc.imageSize;
    $("dWidth").value = sc.imageSize ? sc.imageSize[0] : ""; $("dHeight").value = sc.imageSize ? sc.imageSize[1] : "";
    $("dAnim").checked = !!sc.anim; $("dFrameFirst").value = sc.frameFirst; $("dFrameLast").value = sc.frameLast;
    $("dFrameRow").hidden = !sc.anim;
    $("dAnimHint").textContent = sc.anim ? `frames ${sc.frameFirst}-${sc.frameLast}, files ${sc.name}_NN.png` : "one image per frame, split across workers";
    $("dFrameHint").textContent = sc.anim && sc.framesDone ? `${sc.framesDone} done; a wider range renders only the missing frames` : "";
    const sel = $("dCamera");
    sel.innerHTML = `<option value="">camera saved with scene${sc.savedCamera && sc.camera ? ` (${esc(sc.camera)})` : ""}</option>`;
    if (!sc.savedCamera && sc.camera) sel.add(new Option(sc.camera, sc.camera, true, true));
  }
}

function renderLog() {
  const body = $("logBody");
  if (logTab === "events") {
    body.innerHTML = state.events.slice(-100).map((e) => `<div class="${e.level}">${fmtTime(e.t)} ${e.worker ? "[" + esc(e.worker) + "] " : ""}${esc(e.msg)}</div>`).join("");
  } else {
    body.innerHTML = (state.queue.log || []).map((l) => `<div>${esc(l)}</div>`).join("");
  }
  if (!body._pinned) body.scrollTop = body.scrollHeight;
}

// ---------- file browser ----------
const browser = {
  resolve: null, kind: "scene", multi: false, dirsOnly: false, selectedSet: new Set(), cur: null,
  open({ title, kind = "scene", multi = false, dirsOnly = false, nameRow = false, start = null, defaultName = "" }) {
    this.kind = kind; this.multi = multi; this.dirsOnly = dirsOnly; this.selectedSet = new Set();
    $("bTitle").textContent = title; $("bNameRow").hidden = !nameRow; $("bName").value = defaultName;
    $("browser").hidden = false;
    this.load(start);
    return new Promise((res) => (this.resolve = res));
  },
  async load(dir) {
    try {
      const d = await api(`/api/browse?kind=${this.dirsOnly ? "none" : this.kind}` + (dir ? `&dir=${encodeURIComponent(dir)}` : ""));
      this.cur = d; $("bDir").value = d.dir;
      if (d.fallback) toast(`folder not available, showing ${d.dir}`);
      const drives = $("bDrives");
      drives.innerHTML = d.drives.map((x) => `<option ${d.dir.toUpperCase().startsWith(x.toUpperCase()) ? "selected" : ""}>${x}</option>`).join("");
      $("bList").innerHTML = d.dirs.map((n) => `<div class="entry dir" data-name="${esc(n)}"><span class="ic">▸</span>${esc(n)}</div>`).join("")
        + d.files.map((f) => `<div class="entry file" data-name="${esc(f.name)}"><span class="ic">·</span>${esc(f.name)}<span class="mt">${fmtDate(f.mtime)}</span></div>`).join("");
      this.updateCount();
    } catch (e) { toast(e.message); }
  },
  updateCount() { $("bCount").textContent = this.multi ? `${this.selectedSet.size} selected` : ""; },
  pick() {
    const dir = this.cur.dir;
    if (!$("bNameRow").hidden) { const n = $("bName").value.trim(); return n ? `${dir}/${n}` : null; }
    if (this.dirsOnly) return dir;
    const list = [...this.selectedSet].map((n) => `${dir}/${n}`);
    return this.multi ? list : list[0] || null;
  },
  close(value) { $("browser").hidden = true; const r = this.resolve; this.resolve = null; if (r) r(value); },
};
$("bList").addEventListener("click", (ev) => {
  const el = ev.target.closest(".entry"); if (!el) return;
  if (el.classList.contains("dir")) { browser.load(`${browser.cur.dir.replace(/\/$/, "")}/${el.dataset.name}`); return; }
  if (browser.dirsOnly) return;
  if (browser.multi) {
    if (browser.selectedSet.has(el.dataset.name)) browser.selectedSet.delete(el.dataset.name); else browser.selectedSet.add(el.dataset.name);
    el.classList.toggle("selected");
  } else {
    browser.selectedSet = new Set([el.dataset.name]);
    $("bList").querySelectorAll(".entry.selected").forEach((e) => e.classList.remove("selected"));
    el.classList.add("selected");
    if (!$("bNameRow").hidden) $("bName").value = el.dataset.name;
  }
  browser.updateCount();
});
$("bList").addEventListener("dblclick", (ev) => {
  const el = ev.target.closest(".entry.file"); if (!el || browser.multi) return;
  browser.close(browser.pick());
});
$("bUp").onclick = () => browser.cur && browser.cur.parent && browser.load(browser.cur.parent);
$("bGo").onclick = () => browser.load($("bDir").value.trim());
$("bDir").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); browser.load($("bDir").value.trim()); } });
$("bDrives").onchange = () => browser.load($("bDrives").value);
$("bOk").onclick = () => { const v = browser.pick(); if (v == null || (Array.isArray(v) && !v.length)) { toast("nothing selected"); return; } browser.close(v); };

// ---------- modals ----------
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => {
  const m = b.closest(".modal"); if (m.id === "browser") browser.close(null); else m.hidden = true;
}));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!$("browser").hidden) browser.close(null); else document.querySelectorAll(".modal").forEach((m) => (m.hidden = true)); }
});

// ---------- header actions ----------
$("btnOpen").onclick = $("queueName").onclick = async () => {
  const start = state && state.queue.path ? dirname(state.queue.path) : null;
  const p = await browser.open({ title: "Open queue (.rq.json)", kind: "queue", start });
  if (p) act("/api/queue/open", { path: p }, "queue opened");
};
$("btnNew").onclick = async () => {
  const start = state && state.queue.path ? dirname(state.queue.path) : null;
  const p = await browser.open({ title: "New queue: choose folder and name", kind: "queue", nameRow: true, start, defaultName: "queue.rq.json" });
  if (p) act("/api/queue/new", { path: p }, "queue created");
};
$("btnSaveAs").onclick = async () => {
  const start = state && state.queue.path ? dirname(state.queue.path) : null;
  const p = await browser.open({ title: "Save queue as", kind: "queue", nameRow: true, start, defaultName: base(state.queue.path || "queue.rq.json") });
  if (p) act("/api/queue/save", { path: p }, "saved");
};
$("btnRender").onclick = () => act("/api/run/start", {}, "rendering");
$("btnPause").onclick = () => act("/api/run/pause");
$("btnResume").onclick = () => act("/api/run/resume");
$("btnStop").onclick = () => { if (confirm("Kill the running renders? Their scenes go back to pending.")) act("/api/run/stop"); };
$("btnAbortShutdown").onclick = () => act("/api/shutdown/abort", {}, "shutdown aborted");
setInterval(() => {  // tick the countdown every second between polls
  if (state && state.shutdownAt) { const left = Math.round(state.shutdownAt - (state.now + (Date.now() - lastPollMs) / 1000)); $("shutdownCountdown").textContent = Math.max(0, left); }
}, 1000);
$("shutdownAfter").onchange = (e) => act("/api/settings", { shutdownAfterQueue: e.target.checked });
$("btnResetFailed").onclick = () => act("/api/scenes/reset", { failedOnly: true });
$("btnResetAll").onclick = () => { if (confirm("Reset every scene to pending?")) act("/api/scenes/reset", { failedOnly: false }); };
$("find").oninput = () => state && render();

// ---------- table actions ----------
$("scenes").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  const tr = ev.target.closest("tr[data-i]"); if (!tr) return;
  const i = Number(tr.dataset.i);
  if (ev.target.classList.contains("dnbox")) {
    ev.stopPropagation();
    act(`/api/scene/${i}/denoise`, { on: ev.target.checked });
    return;
  }
  if (ev.target.classList.contains("selbox")) {
    ev.stopPropagation();
    if (ev.shiftKey && anchor != null) selectRange(anchor, i, true);
    else if (ev.target.checked) selection.add(i); else selection.delete(i);
    anchor = i; render();
    return;
  }
  if (btn) {
    ev.stopPropagation();
    const a = btn.dataset.act;
    if (a === "move") { const d = await act(`/api/scene/${i}/move`, { delta: Number(btn.dataset.d) }); if (d && selected === i) selected = d.index; }
    else if (a === "remove") { if (confirm(`Remove ${state.scenes[i].name} from the queue?`)) { await act(`/api/scene/${i}/remove`); if (selected === i) { selected = null; $("details").hidden = true; } } }
    else act(`/api/scene/${i}/${a}`);
    return;
  }
  if (ev.shiftKey && anchor != null) { selectRange(anchor, i, true); }
  else if (ev.ctrlKey || ev.metaKey) { if (selection.has(i)) selection.delete(i); else selection.add(i); }
  else if (selection.has(i)) {               // clicking a selected row again deselects it
    selection.delete(i);
    if (selected === i) { selected = null; $("details").hidden = true; }
    anchor = i; render(); return;
  }
  else { selection = new Set([i]); }
  anchor = i; selected = i; renderDetails(state.scenes[i], true); render();
});

// ---------- drag to reorder ----------
const drag = { active: false, indices: [], over: null, after: false };
const tbodyEl = $("scenes").querySelector("tbody");
tbodyEl.addEventListener("dragstart", (ev) => {
  const tr = ev.target.closest("tr[data-i]"); if (!tr) return;
  const i = Number(tr.dataset.i);
  drag.active = true;
  drag.indices = selection.has(i) ? [...selection].sort((a, b) => a - b) : [i];
  ev.dataTransfer.effectAllowed = "move";
  try { ev.dataTransfer.setData("text/plain", String(i)); } catch (e) {}
  tbodyEl.querySelectorAll("tr").forEach((r) => { if (drag.indices.includes(Number(r.dataset.i))) r.classList.add("dragging"); });
});
tbodyEl.addEventListener("dragover", (ev) => {
  if (!drag.active) return;
  const tr = ev.target.closest("tr[data-i]"); if (!tr) return;
  ev.preventDefault(); ev.dataTransfer.dropEffect = "move";
  const rect = tr.getBoundingClientRect();
  const after = ev.clientY > rect.top + rect.height / 2;
  if (drag.over !== tr || drag.after !== after) {
    if (drag.over) drag.over.classList.remove("drop-before", "drop-after");
    drag.over = tr; drag.after = after;
    tr.classList.add(after ? "drop-after" : "drop-before");
  }
});
tbodyEl.addEventListener("dragleave", (ev) => {
  if (drag.over && !tbodyEl.contains(ev.relatedTarget)) { drag.over.classList.remove("drop-before", "drop-after"); drag.over = null; }
});
tbodyEl.addEventListener("drop", async (ev) => {
  if (!drag.active) return;
  ev.preventDefault();
  const tr = ev.target.closest("tr[data-i]");
  const to = tr ? Number(tr.dataset.i) + (drag.after ? 1 : 0) : state.scenes.length;
  const indices = drag.indices;
  endDrag();
  if (indices.length === 1 && (to === indices[0] || to === indices[0] + 1)) return;  // dropped where it was
  const d = await act("/api/scenes/reorder", { indices, to });
  if (d && d.indices) { selection = new Set(d.indices); if (selected != null) selected = d.indices[0]; render(); }
});
tbodyEl.addEventListener("dragend", () => { endDrag(); render(); });
function endDrag() {
  drag.active = false;
  tbodyEl.querySelectorAll("tr.dragging, tr.drop-before, tr.drop-after").forEach((r) => r.classList.remove("dragging", "drop-before", "drop-after"));
  drag.over = null;
}
function selectRange(a, b, add) {
  const [lo, hi] = a < b ? [a, b] : [b, a];
  if (!add) selection = new Set();
  for (let k = lo; k <= hi; k++) selection.add(k);
}
function renderBatchBar() {
  const n = selection.size;
  const bar = $("batchbar");
  bar.classList.toggle("inactive", n === 0);   // always takes up its space; never shifts the table
  bar.querySelectorAll("button").forEach((b) => (b.disabled = n === 0));
  $("batchCount").textContent = n ? `${n} selected` : "no selection";
  const all = $("selAll"); const total = state ? state.scenes.length : 0;
  all.checked = total > 0 && n === total; all.indeterminate = n > 0 && n < total;
}
$("selAll").onchange = (e) => { selection = e.target.checked ? new Set(state.scenes.map((s) => s.index)) : new Set(); render(); };
$("batchClear").onclick = () => { selection = new Set(); render(); };
$("batchbar").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-batch]"); if (!b) return;
  const indices = [...selection]; if (!indices.length) return;
  const action = b.dataset.batch;
  if (action === "remove" && !confirm(`Remove ${indices.length} scene(s) from the queue?`)) return;
  const d = await act("/api/scenes/batch", { indices, action, on: b.dataset.on !== "0" });
  if (d) {
    toast(`${d.done} scene(s) ${action === "denoise" ? "updated" : action === "remove" ? "removed" : action}${d.skipped ? `, ${d.skipped} rendering skipped` : ""}`, true);
    if (action === "remove") { selection = new Set(); selected = null; $("details").hidden = true; }
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && selection.size && document.querySelectorAll(".modal:not([hidden])").length === 0) { selection = new Set(); render(); }
  if (e.key === "a" && (e.ctrlKey || e.metaKey) && state && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)
      && document.querySelectorAll(".modal:not([hidden])").length === 0) { e.preventDefault(); selection = new Set(state.scenes.map((s) => s.index)); render(); }
});
$("dClose").onclick = () => { selected = null; $("details").hidden = true; render(); };
$("dForm").onsubmit = (e) => {
  e.preventDefault();
  let imageSize = null;
  try { imageSize = sizeFrom("dSizeOn", "dWidth", "dHeight"); } catch (e2) { toast(e2.message); return; }
  act(`/api/scene/${selected}/edit`, { target: $("dTarget").value.trim(), postLoaders: $("dAssets").value.split("\n").map((s) => s.trim()).filter(Boolean), camera: $("dCamera").value, denoise: $("dDenoise").checked,
    anim: $("dAnim").checked, frameFirst: Number($("dFrameFirst").value), frameLast: Number($("dFrameLast").value), imageSize }, "scene saved");
};
$("dAnim").onchange = () => { $("dFrameRow").hidden = !$("dAnim").checked; };
$("dSizeOn").onchange = () => { $("dSizeRow").hidden = !$("dSizeOn").checked; };
$("dReset").onclick = () => act(`/api/scene/${selected}/reset`);
$("dDone").onclick = () => act(`/api/scene/${selected}/done`);
$("dRemove").onclick = async () => { if (confirm("Remove this scene?")) { await act(`/api/scene/${selected}/remove`); selected = null; $("details").hidden = true; render(); } };
$("dLoadCams").onclick = async () => {
  try {
    $("dLoadCams").disabled = true;
    const d = await api(`/api/scene/${selected}/cameras`);
    const sel = $("dCamera"), keep = sel.value;
    sel.innerHTML = `<option value="">camera saved with scene${d.current ? ` (${esc(d.current)})` : ""}</option>` + d.cameras.map((c) => `<option ${c === keep ? "selected" : ""}>${esc(c)}</option>`).join("");
  } catch (e) { toast(e.message); } finally { $("dLoadCams").disabled = false; }
};

// ---------- add scenes ----------
$("btnAdd").onclick = () => {
  const sc = state.scenes[state.scenes.length - 1];
  $("aTargetDir").value = sc && sc.target ? dirname(sc.target) : "";
  $("aAssets").value = "";  // never inherited from earlier scenes
  $("aFiles").value = "";
  $("aAnimBox").hidden = true; $("aAnim").checked = false; $("aFrameRow").hidden = true; animInfoFor = null;
  $("aSizeOn").checked = false; $("aSizeRow").hidden = true; $("aWidth").value = ""; $("aHeight").value = "";
  $("aDenoise").checked = sc ? !!sc.denoise : false;
  $("aDenoiseHint").textContent = state.config.denoiserValid ? "" : "no valid denoiser path in Settings: the flag is kept but skipped";
  $("addDlg").hidden = false;
};
$("aBrowse").onclick = async () => {
  const files = await browser.open({ title: "Pick scene files", kind: "scene", multi: true });
  if (files && files.length) $("aFiles").value = ($("aFiles").value.trim() ? $("aFiles").value.trim() + "\n" : "") + files.join("\n");
  updateAddAnim();
};
// Animation is offered when exactly one scene is being added: the timeline is read from the .duf.
let animInfoFor = null;
async function updateAddAnim() {
  const files = $("aFiles").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const box = $("aAnimBox");
  if (files.length !== 1) { box.hidden = true; $("aAnim").checked = false; animInfoFor = null; return; }
  if (animInfoFor === files[0]) return;
  animInfoFor = files[0];
  // the timeline comes from the .duf itself; big scenes take a few seconds to parse
  box.hidden = false; $("aAnim").checked = false; $("aAnim").disabled = true; $("aFrameRow").hidden = true;
  $("aAnimHint").textContent = "reading the scene's timeline…";
  try {
    const info = await api(`/api/animinfo?file=${encodeURIComponent(files[0])}`);
    if (animInfoFor !== files[0]) return;
    $("aAnim").disabled = false;
    $("aAnimHint").textContent = `${info.changing} of ${info.tracks} tracks change over the play range ${info.frameFirst}-${info.frameLast} at ${info.fps} fps`;
    $("aFrameFirst").value = info.frameFirst; $("aFrameLast").value = info.frameLast;
    $("aFrameHint").textContent = `${info.frameLast - info.frameFirst + 1} frames, files <scene>_NN.png`;
    $("aAnim").checked = false; $("aFrameRow").hidden = true;
  } catch (e) { if (animInfoFor === files[0]) { $("aAnimHint").textContent = `timeline not readable: ${e.message}`; $("aAnim").disabled = true; } }
}
$("aFiles").addEventListener("change", updateAddAnim);
$("aFiles").addEventListener("blur", updateAddAnim);
$("aAnim").onchange = () => { $("aFrameRow").hidden = !$("aAnim").checked; };
$("aSizeOn").onchange = () => { $("aSizeRow").hidden = !$("aSizeOn").checked; };
function sizeFrom(onId, wId, hId) {
  if (!$(onId).checked) return null;
  const w = Number($(wId).value), h = Number($(hId).value);
  if (!(w > 0 && h > 0)) throw new Error("render size needs a positive width and height");
  return [w, h];
}
$("aTargetBrowse").onclick = async () => {
  const d = await browser.open({ title: "Target folder", dirsOnly: true, start: $("aTargetDir").value || null });
  if (d) $("aTargetDir").value = d;
};
$("aAssetBrowse").onclick = async () => {
  const f = await browser.open({ title: "Pick a preset (.duf)", kind: "scene", start: dirname($("aAssets").value.split("\n")[0] || "") || null });
  if (f) $("aAssets").value = ($("aAssets").value.trim() ? $("aAssets").value.trim() + "\n" : "") + f;
};
$("addForm").onsubmit = async (e) => {
  e.preventDefault();
  const files = $("aFiles").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!files.length) { toast("no scene files"); return; }
  const anim = files.length === 1 && !$("aAnimBox").hidden && $("aAnim").checked ? { frameFirst: Number($("aFrameFirst").value), frameLast: Number($("aFrameLast").value) } : null;
  let imageSize = null;
  try { imageSize = sizeFrom("aSizeOn", "aWidth", "aHeight"); } catch (e2) { toast(e2.message); return; }
  const d = await act("/api/scenes/add", { files, targetDir: $("aTargetDir").value.trim() || null, postLoaders: $("aAssets").value.split("\n").map((s) => s.trim()).filter(Boolean), atTop: $("aTop").checked, denoise: $("aDenoise").checked, anim, imageSize });
  if (d) { toast(`${d.added} scene(s) added`, true); $("addDlg").hidden = true; }
};

// ---------- settings ----------
function workerRowHtml(w, gpus) {
  const opts = [`<option value="all" ${w.gpu == null ? "selected" : ""}>all GPUs</option>`]
    .concat(gpus.map((g) => `<option value="${g.index}" ${w.gpu === g.index ? "selected" : ""}>GPU ${g.index}: ${esc(g.name)}</option>`));
  if (w.gpu != null && !gpus.some((g) => String(g.index) === String(w.gpu)))  // pinned, but nvidia-smi lists no such GPU: keep the pin
    opts.push(`<option value="${esc(w.gpu)}" selected>GPU ${esc(w.gpu)} (not detected)</option>`);
  const on = w.enabled !== false;
  return `<div class="worker-row"><label class="wtoggle" title="Enabled: takes part in runs"><input type="checkbox" class="wenabled" ${on ? "checked" : ""}></label><input type="text" class="wname" value="${esc(w.name)}" placeholder="instance name"><select class="wgpu">${opts.join("")}</select><button type="button" class="wdel" title="Remove">✕</button></div>`;
}
$("btnSettings").onclick = () => {
  const c = state.config;
  $("sWorkers").innerHTML = c.workers.map((w) => workerRowHtml(w, state.gpus)).join("");
  document.querySelectorAll("input[name=sMode]").forEach((r) => (r.checked = r.value === (c.mode || "parallel")));
  document.querySelectorAll("input[data-note]").forEach((cb) => (cb.checked = (c.notifications || {})[cb.dataset.note] !== false));
  $("sTimeout").value = c.jobTimeoutS; $("sStall").value = c.stallTimeoutS; $("sAttempts").value = c.maxAttempts;
  $("sDaz").value = c.dazExe; $("sPort").value = c.webPort;
  $("sDenoiser").value = c.denoiserExe || ""; $("sKeepOriginal").checked = !!c.denoiseKeepOriginal;
  lastInstalledExe = (c.denoiserInstall || {}).exe || null;  // an install that finished earlier is already in the field
  renderDenoiserState();
  $("sInfo").textContent = `Work dir: ${c.workDir}` + (state.running ? " · workers can be changed when not rendering" : "");
  $("settingsDlg").hidden = false;
  equalizeSettingsTabs();
};
// All tab panels get the height of the tallest one, so switching tabs never resizes the dialog.
function equalizeSettingsTabs() {
  const panels = [...document.querySelectorAll("#settingsForm .stab")];
  const active = panels.find((d) => !d.hidden);
  panels.forEach((d) => { d.hidden = false; d.style.minHeight = ""; });
  const max = Math.max(...panels.map((d) => d.offsetHeight));
  panels.forEach((d) => { d.style.minHeight = `${max}px`; d.hidden = d !== active; });
}
let lastInstalledExe = null;
function renderDenoiserState() {
  const c = state.config, st = c.denoiserInstall, el = $("sDenoiserState"), btn = $("sDenoiserInstall");
  if (st && !st.done) {
    el.textContent = `⬇ ${st.phase}${st.fraction > 0 && st.fraction < 1 ? ` · ${Math.round(st.fraction * 100)}%` : ""}`;
    el.className = "hint"; btn.disabled = true; btn.textContent = "Installing…";
    return;
  }
  btn.disabled = false; btn.textContent = c.denoiserValid ? "Reinstall…" : "Install…";
  if (st && st.exe && st.exe !== lastInstalledExe) { lastInstalledExe = st.exe; $("sDenoiser").value = st.exe; }
  if (st && st.error) { el.textContent = `✗ install failed: ${st.error}`; el.className = "hint err"; return; }
  const field = $("sDenoiser").value.trim();
  if (st && st.exe && field === st.exe) { el.textContent = `✓ installed ${st.exe}`; el.className = "hint"; return; }
  el.textContent = c.denoiserExe ? (c.denoiserValid ? "✓ found" : "✗ not found: denoise flags are skipped") : "not set: denoise flags are skipped";
  el.className = "hint " + (c.denoiserExe && !c.denoiserValid ? "err" : "");
}
function showSettingsTab(name) {
  document.querySelectorAll("#sTabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.stab === name));
  document.querySelectorAll("#settingsForm .stab").forEach((d) => (d.hidden = d.dataset.stab !== name));
}
$("sTabs").addEventListener("click", (e) => { const b = e.target.closest("[data-stab]"); if (b) showSettingsTab(b.dataset.stab); });
let firstRunShown = false;
function maybeShowSetup() {
  if (!state.firstRun || firstRunShown) return;
  firstRunShown = true;
  try { if (sessionStorage.getItem("setupShown")) return; sessionStorage.setItem("setupShown", "1"); } catch (e) {}
  $("btnSettings").click(); showSettingsTab("setup");
}
$("sDenoiserInstall").onclick = async () => {
  const c = state.config;
  if (!confirm(`Download the latest Denoiser OI release (~50 MB) from GitHub into\n${c.workDir}/denoiser\nand use it as the denoiser?`)) return;
  const d = await act("/api/denoiser/install", {}, "downloading the denoiser…");
  if (d) renderDenoiserState();
};
$("sDenoiserBrowse").onclick = async () => {
  const f = await browser.open({ title: "Pick Denoiser.exe", kind: "exe", start: dirname($("sDenoiser").value) || null });
  if (f) $("sDenoiser").value = f;
};
$("sAddWorker").onclick = () => {
  const n = $("sWorkers").children.length;
  $("sWorkers").insertAdjacentHTML("beforeend", workerRowHtml({ name: `rqgpu${n}`, gpu: state.gpus[n] ? n : null }, state.gpus));
  equalizeSettingsTabs();
};
$("sWorkers").addEventListener("click", (e) => { if (e.target.classList.contains("wdel")) { e.target.closest(".worker-row").remove(); equalizeSettingsTabs(); } });
$("settingsForm").onsubmit = async (e) => {
  e.preventDefault();
  const workers = [...$("sWorkers").querySelectorAll(".worker-row")].map((r) => ({ name: r.querySelector(".wname").value.trim(), gpu: r.querySelector(".wgpu").value, enabled: r.querySelector(".wenabled").checked }));
  const modeEl = document.querySelector("input[name=sMode]:checked");
  const body = { jobTimeoutS: Number($("sTimeout").value), stallTimeoutS: Number($("sStall").value), maxAttempts: Number($("sAttempts").value),
    dazExe: $("sDaz").value.trim(), webPort: Number($("sPort").value),
    denoiserExe: $("sDenoiser").value.trim(), denoiseKeepOriginal: $("sKeepOriginal").checked,
    notifications: Object.fromEntries([...document.querySelectorAll("input[data-note]")].map((cb) => [cb.dataset.note, cb.checked])) };
  if (!state.running) { body.workers = workers; if (modeEl) body.mode = modeEl.value; }
  const newPort = body.webPort !== state.config.webPort ? body.webPort : null;
  const d = await act("/api/settings", body, "settings saved");
  if (d) {
    $("settingsDlg").hidden = true;
    if (newPort) { clearInterval(pollTimer); setTimeout(() => { location.href = `${location.protocol}//${location.hostname}:${newPort}/`; }, 500); }
  }
};

// ---------- log ----------
$("logToggle").onclick = () => document.querySelector(".log").classList.toggle("collapsed");
document.querySelectorAll(".log-tabs .tab").forEach((b) => (b.onclick = () => {
  document.querySelectorAll(".log-tabs .tab").forEach((x) => x.classList.remove("active")); b.classList.add("active");
  logTab = b.dataset.tab; renderLog();
}));
$("logBody").addEventListener("scroll", (e) => { const b = e.target; b._pinned = b.scrollTop + b.clientHeight < b.scrollHeight - 10; });

// ---------- start ----------
refresh();
pollTimer = setInterval(refresh, 2000);
