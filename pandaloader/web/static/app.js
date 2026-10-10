/* PandaLoader web UI. Vanilla JS, polls /api/state every 2 s. */
"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let selected = null;          // selected scene index
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
async function refresh() {
  try {
    state = await api("/api/state");
    render();
  } catch (e) {
    $("summary").textContent = "connection lost";
  }
}

function render() {
  const s = state, q = s.queue;
  // header
  $("queueName").textContent = q.path ? base(q.path).replace(/\.rq\.json$/, "") : "no queue open";
  $("queuePath").textContent = q.path || "";
  const etaTxt = s.running ? ` · ETA <b>${hms(q.eta)}</b>` : (s.finished ? " · finished" : "");
  $("summary").innerHTML = q.total ? `<b>${q.done}</b>/${q.total} done${q.failed ? ` · <b class="err">${q.failed} failed</b>` : ""}${etaTxt}` : "";
  $("btnRender").hidden = s.running;
  $("btnPause").hidden = !s.running || s.paused;
  $("btnResume").hidden = !s.running || !s.paused;
  $("btnStop").hidden = !s.running;
  $("btnRender").disabled = !q.path || q.pending === 0;
  $("shutdownAfter").checked = !!s.shutdownAfterQueue;
  $("counts").textContent = q.total ? `${q.pending} pending · ${q.running} running · ${q.done} done · ${q.failed} failed` + (q.averageSecs ? ` · avg ${hms(q.averageSecs)}` : "") : "";

  // workers
  $("workers").innerHTML = s.workers.map(workerCard).join("");

  // table
  const find = $("find").value.trim().toLowerCase();
  const tb = $("scenes").querySelector("tbody");
  tb.innerHTML = s.scenes.map((sc) => sceneRow(sc, find)).join("");
  $("emptyHint").hidden = s.scenes.length > 0;
  if (selected != null && !s.scenes[selected]) { selected = null; $("details").hidden = true; }
  if (selected != null) renderDetails(s.scenes[selected], false);

  // log
  renderLog();
}

// ---------- GPU gauges ----------
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
  else if (st === "rendering") frac = 0.1 + 0.88 * Math.min(1, w.expectedSecs ? w.renderElapsed / w.expectedSecs : 0);
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
    <div class="scene">${esc(w.sceneName)}<span class="cam">${esc(w.camera || "")}</span></div>
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
  const cls = [sc.index === selected ? "selected" : "", found ? "found" : ""].join(" ");
  const stateTxt = sc.stateName === "running" && sc.worker ? `${sc.worker}` : sc.stateName;
  const cam = sc.savedCamera ? (sc.camera ? `${esc(sc.camera)} <span class="hint">(saved)</span>` : `<span class="hint">saved with scene</span>`) : `<b>${esc(sc.camera)}</b>`;
  const assets = sc.postLoaders.length ? `${sc.postLoaders.length} PRA` : "";
  const last = sc.lastSecs ? hms(sc.lastSecs) : "";
  const err = sc.lastError ? ` title="${esc(sc.lastError)}"` : "";
  return `<tr class="${cls}" data-i="${sc.index}"${err}>
    <td class="num">${sc.index + 1}</td>
    <td class="state"><span class="badge ${sc.stateName}">${esc(stateTxt)}</span></td>
    <td>${esc(sc.name)}.duf</td>
    <td class="hint">${fmtDate(sc.mtime)}</td>
    <td>${cam}</td>
    <td>${last}</td>
    <td class="hint">${assets}</td>
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
  $("dState").innerHTML = `<span class="badge ${sc.stateName}">${sc.stateName}</span>` + (sc.lastSecs ? ` last render ${hms(sc.lastSecs)}` : "");
  $("dError").textContent = sc.lastError || "";
  const img = $("dThumb");
  if (sc.targetExists) {
    const src = `/api/scene/${sc.index}/thumb?w=800`;
    if (img.dataset.src !== src) { img.src = src; img.dataset.src = src; }
    img.hidden = false; $("dNoThumb").hidden = true; $("dThumbLink").href = `/api/scene/${sc.index}/image`;
  } else { img.hidden = true; img.dataset.src = ""; $("dNoThumb").hidden = false; $("dThumbLink").removeAttribute("href"); }
  if (full || d.dataset.index !== String(sc.index)) {
    d.dataset.index = sc.index;
    $("dTarget").value = sc.target || "";
    $("dAssets").value = sc.postLoaders.join("\n");
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
$("shutdownAfter").onchange = (e) => act("/api/settings", { shutdownAfterQueue: e.target.checked });
$("btnResetFailed").onclick = () => act("/api/scenes/reset", { failedOnly: true });
$("btnResetAll").onclick = () => { if (confirm("Reset every scene to pending?")) act("/api/scenes/reset", { failedOnly: false }); };
$("find").oninput = () => state && render();

// ---------- table actions ----------
$("scenes").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  const tr = ev.target.closest("tr[data-i]"); if (!tr) return;
  const i = Number(tr.dataset.i);
  if (btn) {
    ev.stopPropagation();
    const a = btn.dataset.act;
    if (a === "move") { const d = await act(`/api/scene/${i}/move`, { delta: Number(btn.dataset.d) }); if (d && selected === i) selected = d.index; }
    else if (a === "remove") { if (confirm(`Remove ${state.scenes[i].name} from the queue?`)) { await act(`/api/scene/${i}/remove`); if (selected === i) { selected = null; $("details").hidden = true; } } }
    else act(`/api/scene/${i}/${a}`);
    return;
  }
  selected = i; renderDetails(state.scenes[i], true); render();
});
$("dClose").onclick = () => { selected = null; $("details").hidden = true; render(); };
$("dForm").onsubmit = (e) => {
  e.preventDefault();
  act(`/api/scene/${selected}/edit`, { target: $("dTarget").value.trim(), postLoaders: $("dAssets").value.split("\n").map((s) => s.trim()).filter(Boolean), camera: $("dCamera").value }, "scene saved");
};
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
  $("aAssets").value = sc ? sc.postLoaders.join("\n") : "";
  $("aFiles").value = "";
  $("addDlg").hidden = false;
};
$("aBrowse").onclick = async () => {
  const files = await browser.open({ title: "Pick scene files", kind: "scene", multi: true });
  if (files && files.length) $("aFiles").value = ($("aFiles").value.trim() ? $("aFiles").value.trim() + "\n" : "") + files.join("\n");
};
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
  const d = await act("/api/scenes/add", { files, targetDir: $("aTargetDir").value.trim() || null, postLoaders: $("aAssets").value.split("\n").map((s) => s.trim()).filter(Boolean), atTop: $("aTop").checked });
  if (d) { toast(`${d.added} scene(s) added`, true); $("addDlg").hidden = true; }
};

// ---------- settings ----------
function workerRowHtml(w, gpus) {
  const opts = [`<option value="all" ${w.gpu == null ? "selected" : ""}>all GPUs</option>`]
    .concat(gpus.map((g) => `<option value="${g.index}" ${w.gpu === g.index ? "selected" : ""}>GPU ${g.index}: ${esc(g.name)}</option>`));
  const on = w.enabled !== false;
  return `<div class="worker-row"><label class="wtoggle" title="Enabled: takes part in runs"><input type="checkbox" class="wenabled" ${on ? "checked" : ""}></label><input type="text" class="wname" value="${esc(w.name)}" placeholder="instance name"><select class="wgpu">${opts.join("")}</select><button type="button" class="wdel" title="Remove">✕</button></div>`;
}
$("btnSettings").onclick = () => {
  const c = state.config;
  $("sWorkers").innerHTML = c.workers.map((w) => workerRowHtml(w, state.gpus)).join("");
  document.querySelectorAll("input[name=sMode]").forEach((r) => (r.checked = r.value === (c.mode || "parallel")));
  document.querySelectorAll("input[data-note]").forEach((cb) => (cb.checked = (c.notifications || {})[cb.dataset.note] !== false));
  $("sTimeout").value = c.jobTimeoutS; $("sStall").value = c.stallTimeoutS; $("sAttempts").value = c.maxAttempts;
  $("sSize").value = c.imageSize ? c.imageSize.join("x") : ""; $("sDaz").value = c.dazExe; $("sPort").value = c.webPort;
  $("sInfo").textContent = `Work dir: ${c.workDir}` + (state.running ? " · workers can be changed when not rendering" : "");
  $("settingsDlg").hidden = false;
};
$("sAddWorker").onclick = () => {
  const n = $("sWorkers").children.length;
  $("sWorkers").insertAdjacentHTML("beforeend", workerRowHtml({ name: `rqgpu${n}`, gpu: state.gpus[n] ? n : null }, state.gpus));
};
$("sWorkers").addEventListener("click", (e) => { if (e.target.classList.contains("wdel")) e.target.closest(".worker-row").remove(); });
$("settingsForm").onsubmit = async (e) => {
  e.preventDefault();
  const workers = [...$("sWorkers").querySelectorAll(".worker-row")].map((r) => ({ name: r.querySelector(".wname").value.trim(), gpu: r.querySelector(".wgpu").value, enabled: r.querySelector(".wenabled").checked }));
  const modeEl = document.querySelector("input[name=sMode]:checked");
  const size = $("sSize").value.trim();
  const body = { jobTimeoutS: Number($("sTimeout").value), stallTimeoutS: Number($("sStall").value), maxAttempts: Number($("sAttempts").value),
    imageSize: size ? size.toLowerCase().split("x").map(Number) : [], dazExe: $("sDaz").value.trim(), webPort: Number($("sPort").value),
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
