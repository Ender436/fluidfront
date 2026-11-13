// Fluidfront WebUI: four-screen wizard (Cases -> Import/Conditions -> Run ->
// Results) talking to the FastAPI server over REST + a WebSocket event bus.
// No framework, no build step: plain ES modules.

import { Viewer } from "/viewer.js";
import { plot } from "/plot.js";

const $ = id => document.getElementById(id);
const state = {
  cases: new Map(),      // name -> {name,state,percent,...}
  current: null,         // selected case name
  viewer: null,
  ws: null,
  snapSel: {},           // case -> {u:[...], rho:[...], flags:[...]}
};

// ------------------------------------------------------------------ utils --
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;",
    ">": "&gt;", '"': "&quot;" }[c]));
}
function fmtTime(s) {
  if (!isFinite(s)) return "?";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}
function fmtN(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + " k";
  return String(n);
}
function facts(el, pairs) {
  el.innerHTML = pairs.map(([v, l]) =>
    `<div class="fact"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail ?? msg; } catch { /* noop */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}

// ------------------------------------------------------------------ router --
function show(screen) {
  for (const s of document.querySelectorAll(".screen"))
    s.classList.toggle("on", s.id === screen);
  for (const b of document.querySelectorAll("#steps button"))
    b.classList.toggle("on", b.dataset.screen === screen);
  if (screen === "results") resultsScreen();
  if (screen === "run") runScreen();
  if (screen === "home") renderCases();
}

// -------------------------------------------------------------- case cards --
function renderCases() {
  const grid = $("casegrid");
  if (!state.cases.size) {
    grid.innerHTML = `<p class="muted">No simulations yet — drop in a 3D
      model and pick a wind speed. That's the whole setup.</p>`;
    return;
  }
  grid.innerHTML = "";
  for (const c of state.cases.values()) {
    const d = document.createElement("div");
    d.className = "card case";
    d.innerHTML = `<div class="name">${esc(c.name)}
        <span class="state-badge s-${c.state}">${c.state}</span></div>
      <div class="st">${c.percent ? c.percent + "% · " : ""}${esc(c.updated || "")}</div>
      <button class="del" title="delete">✕</button>`;
    d.onclick = () => { state.current = c.name; show("run"); };
    d.querySelector(".del").onclick = async e => {
      e.stopPropagation();
      if (!confirm(`Delete case "${c.name}"?`)) return;
      try {
        await api(`/api/cases/${c.name}`, { method: "DELETE" });
        state.cases.delete(c.name);
        if (state.current === c.name) state.current = null;
        renderCases();
      } catch (err) { alert(err.message); }
    };
    grid.appendChild(d);
  }
}

// ------------------------------------------------------------ websocket ---
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.kind === "hello") {
      $("devchip").textContent = ev.device.name;
      for (const c of ev.cases) state.cases.set(c.name, c);
      renderCases();
      if (state.current) { runScreen(); if (ev.active === state.current) pollRunFacts(); }
    } else if (ev.kind === "status") {
      const prev = state.cases.get(ev.case) || {};
      state.cases.set(ev.case, { ...prev, ...ev, name: ev.case });
      renderCases();
      if (ev.case === state.current) {
        runScreen();
        if (ev.state === "running" || ev.state === "building") pollRunFacts();
        if (ev.state === "done") onRunDone();
      }
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2000); // server restarted? retry
}

// ------------------------------------------------------------------- form --
let chosenFile = null;
function initForm() {
  const drop = $("drop"), file = $("file");
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("hover"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("hover"));
  drop.addEventListener("drop", e => {
    e.preventDefault(); drop.classList.remove("hover");
    if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  });
  file.onchange = () => file.files.length && setFile(file.files[0]);
  function setFile(f) {
    chosenFile = f;
    $("fileinfo").textContent = `${f.name} · ${(f.size / 1024).toFixed(0)} KiB`;
  }

  $("form").onsubmit = async e => {
    e.preventDefault();
    const err = $("newerr");
    err.classList.add("hidden");
    if (!chosenFile) {
      err.textContent = "Please choose a 3D model file first.";
      err.classList.remove("hidden");
      return;
    }
    const fd = new FormData();
    fd.append("model", chosenFile);
    fd.append("u_si", $("u_si").value);
    fd.append("tier", document.querySelector("input[name=tier]:checked").value);
    fd.append("fluid", $("fluid").value);
    fd.append("rotation_deg", `${$("rx").value},${$("ry").value},${$("rz").value}`);
    fd.append("size_lu", $("size_lu").value || "0");
    fd.append("model_units", $("model_units").value);
    fd.append("precision", $("precision").value);
    $("btn-create").disabled = true;
    $("btn-create").textContent = "Checking model…";
    try {
      const res = await api("/api/cases", { method: "POST", body: fd });
      showReport(res);
    } catch (err2) {
      err.textContent = err2.message;
      err.classList.remove("hidden");
    } finally {
      $("btn-create").disabled = false;
      $("btn-create").textContent = "Create simulation";
    }
  };

  $("btn-gorun").onclick = () => {
    if (state.current) doRun();
    show("run");
  };
}

function showReport(res) {
  state.current = res.name;
  // the WS "created" event may still be in flight; make the Run screen work
  // immediately after a create (it looks the case up in this map)
  state.cases.set(res.name, { name: res.name, state: "created", percent: 0 });
  $("report").classList.remove("hidden");
  $("rpt-name").textContent = res.name;
  const rep = res.report, d = res.derived;
  facts($("rpt-facts"), [
    [`Re ≈ ${Math.round(d.re_si).toLocaleString()}`, "Reynolds number"],
    [`${(+rep.length_si).toPrecision(3)} m`, "model length"],
    [fmtN(rep.cells), "cells"],
    [`${rep.domain.nx}×${rep.domain.ny}×${rep.domain.nz}`, "domain"],
    [rep.size_lu, "voxels on model"],
    [`${(rep.blockage * 100).toFixed(1)} %`, "tunnel blockage"],
    [d.tau.toFixed(3), "solver stability (tau)"],
    [`≈ ${fmtTime(rep.estimate_s)}`, "estimated run time"],
  ]);
  const warns = [...(d.warnings || []), ...(rep.warnings || [])];
  $("rpt-warns").innerHTML = warns.map(w => `<li>${esc(w)}</li>`).join("");
  $("report").scrollIntoView({ behavior: "smooth" });
}

// --------------------------------------------------------------- run screen -
function runScreen() {
  const c = state.cases.get(state.current);
  $("run-title").textContent = c ? `Run — ${c.name}` : "Run";
  if (!c) return;
  const st = c.state;
  $("barfill").style.width = (c.percent || 0) + "%";
  $("btn-run").disabled = st === "running" || st === "building";
  $("btn-stop").disabled = st !== "running" && st !== "building";
  const bits = [`<div class="fact"><b>${esc(st)}</b><span>status</span></div>`];
  if (c.t_steps) bits.push(`<div class="fact"><b>${c.t || 0} / ${c.t_steps}</b><span>steps</span></div>`);
  if (c.mlups) bits.push(`<div class="fact"><b>${c.mlups}</b><span>MLUPs/s</span></div>`);
  if (c.eta) bits.push(`<div class="fact"><b>${esc(c.eta)}</b><span>left</span></div>`);
  if (c.error) bits.push(`<div class="fact"><b class="err">${esc(c.error)}</b><span>error</span></div>`);
  $("runfacts").innerHTML = bits.join("");
}

async function pollRunFacts() {
  // pull once directly (progress events are throttled; this keeps UI fresh)
  try {
    const detail = await api(`/api/cases/${state.current}`);
    if (detail.status) {
      state.cases.set(state.current, { ...state.cases.get(state.current),
        name: state.current, ...detail.status });
      runScreen();
    }
  } catch { /* server may be mid-rebuild */ }
}

async function doRun() {
  $("runerr").classList.add("hidden");
  try {
    await api(`/api/cases/${state.current}/run`, { method: "POST" });
  } catch (e) {
    $("runerr").textContent = e.message;
    $("runerr").classList.remove("hidden");
  }
}

function onRunDone() {
  // gentle auto-jump: refresh forces+viewer if user is already on results
  if (document.getElementById("results").classList.contains("on")) resultsScreen();
}

// ----------------------------------------------------------- results screen -
async function resultsScreen() {
  const name = state.current;
  $("res-title").textContent = name ? `Results — ${name}` : "Results";
  if (!name) return;
  if (!state.viewer) { state.viewer = new Viewer($("viewer")); window.__viewer = state.viewer;
    state.viewer.onInfo = (s) => { $("cdnote").textContent = s; }; }

  // snapshots -> time selector (Q view rides the velocity snapshots)
  try {
    state.snapSel[name] = await api(`/api/cases/${name}/snapshots`);
    const snaps = state.snapSel[name][snapKey()] || [];
    const sel = $("f-time");
    sel.innerHTML = snaps.map(t => `<option value="${t}">${t}</option>`).reverse().join("");
    if (snaps.length) sel.value = String(snaps[snaps.length - 1]);
  } catch { /* no output yet */ }

  await loadFrame();
  await loadForces();
}

async function loadFrame() {
  const name = state.current;
  if (!name || !state.viewer) return;
  const t = $("f-time").value || null;
  try {
    const info = await state.viewer.load(name, t ? Number(t) : null);
    $("cdnote").textContent = info;
  } catch (e) {
    $("cdnote").textContent = e.message;
  }
}

async function loadForces() {
  const name = state.current;
  if (!name) return;
  try {
    const f = await api(`/api/cases/${name}/forces`);
    const stats = plot($("chart"), f);
    // fewer than two force samples -> plot() returns null; don't try to call
    // .toFixed() on undefined while a run has just started
    const tail = stats && Number.isFinite(stats.cd_tail) ? stats.cd_tail : null;
    const spread = stats && Number.isFinite(stats.cd_spread) ? stats.cd_spread : null;
    const noisy = tail !== null && spread !== null &&
      spread > 0.1 * Math.max(0.1, Math.abs(tail));
    facts($("resfacts"), [
      [tail !== null ? (noisy ? `${tail.toFixed(2)} ± ${spread.toFixed(2)}`
                              : tail.toFixed(3)) : "—", "Cd (last third avg)"],
      [(f.t || []).length, "force samples"],
    ]);
    if (noisy) {
      $("resfacts").innerHTML += `<div class="fact"><span>The wake is still
        oscillating at the end of the run — treat this Cd as an average of a
        flaky flow. Run longer, or use a higher quality setting.</span></div>`;
    }
  } catch (e) {
    $("resfacts").innerHTML = `<span class="muted">${esc(e.message)}</span>`;
  }
}

// ------------------------------------------------------------- wiring ------
for (const b of document.querySelectorAll("#steps button"))
  b.onclick = () => show(b.dataset.screen);
$("btn-new").onclick = () => show("new");
$("btn-run").onclick = doRun;
$("btn-stop").onclick = async () => {
  try {
    await api(`/api/cases/${state.current}/stop`, { method: "POST" });
  } catch (e) {
    const err = $("runerr");
    err.textContent = e.message;
    err.classList.remove("hidden");
  }
};
$("btn-results").onclick = () => show("results");

$("f-which").onchange = async () => {
  const w = $("f-which").value, q = w === "q", st = w === "stream";
  $("l-cmap").classList.toggle("hidden", q); // Q surface: normals, no map
  $("l-wake").classList.toggle("hidden", q || w === "rho"); // wake = u/stream
  $("l-slice").classList.toggle("hidden", q);
  $("l-size").classList.toggle("hidden", q || st); // streamlines: fixed width
  // streamlines use the threshold slider only as the wake cut ("hide fast flow")
  $("l-thresh").classList.toggle("hidden", st && !$("f-wake").checked);
  $("l-lines").classList.toggle("hidden", !st);
  // collapse emptied group boxes so no empty frame lingers
  $("g-slice").classList.toggle("hidden", q);
  $("g-density").classList.toggle("hidden", q);
  if (q && +$("f-thresh").value < 20) $("f-thresh").value = 45;
  state.viewer?.setSettings({ field: w, thresh: $("f-thresh").value / 100,
                              lines: +$("f-lines").value });
  threshLabel();
  const snaps = (state.snapSel[state.current] || {})[snapKey()] || [];
  $("f-time").innerHTML = snaps.map(t => `<option value="${t}">${t}</option>`).reverse().join("");
  loadFrame();
};
// streamline seeds live on the u snapshots; rho/flags have their own lists
const snapKey = () => ["q", "stream"].includes($("f-which").value) ? "u"
  : $("f-which").value;
$("f-lines").onchange = () => {
  state.viewer?.setSettings({ lines: +$("f-lines").value });
  loadFrame(); // line count changes server-side seeding => refetch
};
$("f-cmap").onchange = () => state.viewer?.setSettings({ cmap: $("f-cmap").value });
$("f-thresh").oninput = () => {
  state.viewer?.setSettings({ thresh: $("f-thresh").value / 100 });
  threshLabel();
};
// wake mode: slider now means "% of incoming speed" - start somewhere useful
$("f-wake").onchange = () => {
  if ($("f-wake").checked && +$("f-thresh").value < 20) $("f-thresh").value = 60;
  if ($("f-which").value === "stream") // wake is the streamline threshold knob
    $("l-thresh").classList.toggle("hidden", !$("f-wake").checked);
  state.viewer?.setSettings({ wake: $("f-wake").checked,
                              thresh: $("f-thresh").value / 100 });
  threshLabel();
};
function threshLabel() {
  const v = $("f-thresh").value;
  if ($("f-which").value === "q") {
    const iso = state.viewer?.isoValue;
    $("thresh-lab").textContent = iso ? `eddies ≥${Math.round(iso)}/s²`
                                      : "vortex strength";
    return;
  }
  const which = $("f-which").value;
  $("thresh-lab").textContent =
    which !== "u" && which !== "stream" ? `hide calm (<${v}%)`
    : $("f-wake").checked ? `keep wake (<${v}% inflow)`
    : `hide slow (<${v}%)`;
}
threshLabel();
$("f-size").oninput = () => state.viewer?.setSettings({ pointSize: $("f-size").value });
$("f-slice").onchange = () => {
  $("f-slice-pos").disabled = $("f-slice").value === "";
  state.viewer?.setSettings({ sliceAxis: $("f-slice").value });
};
$("f-slice-pos").oninput = () =>
  state.viewer?.setSettings({ slicePos: $("f-slice-pos").value / 100 });
$("f-detail").onchange = () => {
  state.viewer?.setSettings({ detail: $("f-detail").value });
  if (state.viewer) state.viewer.stamp = null; // rebuild model at new voxel budget
  loadFrame(); // new budget => refetch at the new density
};
$("f-solid").onchange = async () => {
  const on = $("f-solid").checked;
  state.viewer?.setSettings({ showSolid: on }); // also just shows/hides it
  if (on && state.viewer) await state.viewer.ensureSolid(); // fetch if needed
};
$("f-time").onchange = loadFrame;
$("f-refresh").onclick = () => { loadFrame(); loadForces(); };

// ------------------------------------------------------------- video export -
function jpegBytes(dataUrl) {
  const bin = atob(dataUrl.slice(dataUrl.indexOf(",") + 1));
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return u8;
}

$("f-export").onclick = async () => {
  const v = state.viewer, name = state.current;
  const st = $("v-status"), dl = $("v-dl"), btn = $("f-export");
  const bar = $("v-prog"), fill = $("v-progfill");
  const setBar = (p) => { bar.classList.add("on"); fill.style.width = `${Math.round(p)}%`; };
  if (!v || !name) { st.textContent = "open a run first"; return; }
  const fmt = $("f-vfmt").value, fps = +$("f-vfps").value;
  let frames = (((state.snapSel[name] || {})[snapKey()] || [])).slice().sort((a, b) => a - b);
  if (frames.length < 2) {
    st.textContent = "need at least two snapshots (check the run's snapshot setting)";
    return;
  }
  if (frames.length > 300) { // server cap: keep every k-th, evenly
    const k = Math.ceil(frames.length / 300);
    frames = frames.filter((_, i) => i % k === 0);
  }
  btn.disabled = true;
  dl.innerHTML = "";
  st.textContent = "starting capture…";
  setBar(0);
  try {
    await api(`/api/cases/${name}/export/begin`, { method: "POST" });
    for (let i = 0; i < frames.length; i++) {
      st.textContent = `capturing frame ${i + 1}/${frames.length}…`;
      btn.textContent = `making… ${i + 1}/${frames.length}`;
      setBar(100 * (i + 1) / frames.length);
      await v.load(name, frames[i]);
      const bytes = jpegBytes(v.capture(1280, 720));
      await api(`/api/cases/${name}/export/frame?i=${i}`,
                { method: "POST", headers: { "Content-Type": "image/jpeg" },
                  body: bytes });
    }
    st.textContent = "encoding… (a few seconds)";
    btn.textContent = "encoding…";
    const res = await api(`/api/cases/${name}/export/finish`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fps, fmt }),
    });
    st.textContent = "";
    const mb = res.size ? ` · ${(res.size / 1048576).toFixed(1)} MB` : "";
    dl.innerHTML = `<a href="${res.url}" download="${esc(name)}.${fmt}">` +
      `⬇ ${fmt.toUpperCase()} · ${res.frames} frames · ${res.seconds}s${mb}</a>`;
  } catch (e) {
    st.textContent = "export failed: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "make video";
    bar.classList.remove("on");
    fill.style.width = "0%";
    loadFrame(); // viewer back to the frame selected in the time selector
  }
};

// ------------------------------------------------- one-page summary export -
// A standalone HTML file (inline CSS + data-URL images): numbers, caveats,
// the force chart, and a still of the view exactly as the user sees it.
// Self-contained so anyone can open it without FluidFront running.
$("f-summary").onclick = async () => {
  const name = state.current;
  if (!name || !state.viewer) return;
  const btn = $("f-summary"), st = $("v-status");
  btn.disabled = true;
  try {
    const [detail, f] = await Promise.all([
      api(`/api/cases/${name}`), api(`/api/cases/${name}/forces`)]);
    const stats = plot($("chart"), f); // same tail-mean math as the chart
    const still = state.viewer.capture(1280, 720); // sync: see viewer.capture
    const chart = $("chart").toDataURL("image/png");
    const html = summaryHtml(name, detail, f, stats, still, chart);
    window.__lastSummary = html; // debugging hook, mirrors window.__viewer
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    a.download = `${name}-summary.html`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 30000);
    st.textContent = "summary saved";
    setTimeout(() => { if (st.textContent === "summary saved") st.textContent = ""; }, 4000);
  } catch (e) {
    st.textContent = "summary failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
};

function tailMean(vals) { // mirror of plot.js Cd tail window (last third)
  if (!vals || !vals.length) return null;
  const seg = vals.slice(Math.floor(vals.length * 2 / 3));
  return seg.reduce((a, b) => a + b, 0) / seg.length;
}

function summaryHtml(name, detail, f, stats, still, chart) {
  const c = detail.case || {}, rep = detail.report || {};
  const status = detail.status || {};
  // kinematic viscosities kept in sync with fluidfront/gen/units.py FLUIDS
  const NU = { air: 1.516e-5, water: 1.004e-6 };
  const fluid = (c.fluid && c.fluid.kind) || "air";
  const nu = NU[String(fluid).toLowerCase()] || NU.air;
  const L = c.flow && c.flow.length_si, U = c.flow && c.flow.u_si;
  const re = L && U ? (U * L / nu) : null;
  const tail = stats && Number.isFinite(stats.cd_tail) ? stats.cd_tail : null;
  const spread = stats && Number.isFinite(stats.cd_spread) ? stats.cd_spread : null;
  const noisy = tail !== null && spread !== null &&
    spread > 0.1 * Math.max(0.1, Math.abs(tail));
  const cdTxt = tail === null ? "—"
    : tail.toFixed(2) + (noisy ? ` ± ${spread.toFixed(2)}` : "");
  const cl = tailMean(f.cl);
  const cells = rep.cells || (c.domain && c.domain.nx * c.domain.ny * c.domain.nz);
  const secs = status.started && status.updated
    ? Math.round((Date.parse(status.updated) - Date.parse(status.started)) / 1000)
    : null;
  const facts = [
    [esc(cdTxt), "Cd (last-third average" + (noisy ? ", oscillating wake" : "") + ")"],
    [cl === null ? "—" : cl.toFixed(3), "Cl (last-third average)"],
    [re === null ? "—" : re.toExponential(2).replace("e+", "e"), "Reynolds number"],
    [L ? `${L.toFixed(2)} m at ${U.toFixed(1)} m/s` : "—", "model in flow"],
    [cells ? cells.toLocaleString() : "—", "lattice cells"],
    [rep.size_lu ? `${Math.round(rep.size_lu)} voxels across model` : "—", "resolution"],
    [rep.blockage ? `${(rep.blockage * 100).toFixed(1)}%` : "—", "wind-tunnel blockage"],
    [secs !== null ? `${Math.floor(secs / 60)}m ${secs % 60}s` : "—", "solve time"],
    [status.mlups ? `${status.mlups} MLUPs` : "—", "solver speed"],
  ];
  const warns = (rep.warnings || []).concat(
    noisy ? ["The wake was still oscillating when the run ended — this Cd is "
             + "an average of a flaky flow; run longer or raise quality."] : []);
  const rows = facts.map(([v, k]) =>
    `<div class="f"><b>${v}</b><span>${k}</span></div>`).join("");
  const warn = warns.length ? `<h3>Notes on accuracy</h3><ul>` +
    warns.map(w => `<li>${esc(w)}</li>`).join("") + `</ul>` : "";
  const date = new Date().toISOString().slice(0, 10);
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<title>${esc(name)} — FluidFront results</title><style>
body { font-family: system-ui, "Segoe UI", Roboto, sans-serif; margin: 0;
  background: #f4f6f8; color: #1c222b; }
main { max-width: 860px; margin: 24px auto; padding: 0 20px 40px; }
h1 { font-size: 1.5rem; margin-bottom: 2px; } .sub { color: #5a6675; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 10px; margin: 18px 0; }
.f { background: #fff; border: 1px solid #d5dbe3; border-radius: 10px;
  padding: 10px 14px; } .f b { display: block; font-size: 1.25rem; }
.f span { color: #5a6675; font-size: .85rem; }
img { width: 100%; border: 1px solid #d5dbe3; border-radius: 10px;
  margin: 8px 0; } ul { margin: 8px 0; } li { margin: 4px 0; color: #6b4a00; }
footer { color: #5a6675; font-size: .85rem; margin-top: 24px; }
</style></head><body><main>
<h1>${esc(name)}</h1>
<p class="sub">FluidFront CFD summary · ${date} · ${esc(String(c.fluid &&
  c.fluid.kind || "air"))} · ${c.options && c.options.precision ?
  c.options.precision : ""} solver</p>
<div class="grid">${rows}</div>
${warn}
<h3>Flow at t=${esc(String(status.t != null ? status.t : ""))}</h3>
<img src="${still}" alt="flow view">
<h3>Force coefficients over time</h3>
<img src="${chart}" alt="Cd/Cl chart">
<footer>Cd/Cl measured on the voxelised body surface, referenced to its
projected area and model length. Generated by FluidFront — Lattice-Boltzmann
CFD (FluidX3D engine).</footer>
</main></body></html>`;
}

initForm();
connectWS();
window.__viewer = null; // handy for debugging; set when created
