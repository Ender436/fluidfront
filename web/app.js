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
  if (!state.viewer) { state.viewer = new Viewer($("viewer")); window.__viewer = state.viewer; }

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
    const tail = stats?.cd_tail, spread = stats?.cd_spread;
    const noisy = spread !== null && spread > 0.1 * Math.max(0.1, Math.abs(tail));
    facts($("resfacts"), [
      [tail !== null ? (noisy ? `${tail.toFixed(2)} ± ${spread.toFixed(2)}`
                              : tail.toFixed(3)) : "—", "Cd (last third avg)"],
      [f.t.length, "force samples"],
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
$("btn-stop").onclick = () => api(`/api/cases/${state.current}/stop`, { method: "POST" });
$("btn-results").onclick = () => show("results");

$("f-which").onchange = async () => {
  const q = $("f-which").value === "q";
  for (const id of ["l-cmap", "l-wake", "l-size"])
    $(id).classList.toggle("hidden", q);
  if (q && +$("f-thresh").value < 20) $("f-thresh").value = 45;
  state.viewer?.setSettings({ field: $("f-which").value,
                              thresh: $("f-thresh").value / 100 });
  threshLabel();
  const snaps = (state.snapSel[state.current] || {})[snapKey()] || [];
  $("f-time").innerHTML = snaps.map(t => `<option value="${t}">${t}</option>`).reverse().join("");
  loadFrame();
};
const snapKey = () => $("f-which").value === "q" ? "u" : $("f-which").value;
$("f-cmap").onchange = () => state.viewer?.setSettings({ cmap: $("f-cmap").value });
$("f-thresh").oninput = () => {
  state.viewer?.setSettings({ thresh: $("f-thresh").value / 100 });
  threshLabel();
};
// wake mode: slider now means "% of incoming speed" - start somewhere useful
$("f-wake").onchange = () => {
  if ($("f-wake").checked && +$("f-thresh").value < 20) $("f-thresh").value = 60;
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
  $("thresh-lab").textContent =
    $("f-which").value !== "u" ? `hide calm (<${v}%)`
    : $("f-wake").checked ? `keep wake (<${v}% inflow)`
    : `hide slow (<${v}%)`;
}
threshLabel();
$("f-size").oninput = () => state.viewer?.setSettings({ pointSize: $("f-size").value });
$("f-solid").onchange = async () => {
  const on = $("f-solid").checked;
  state.viewer?.setSettings({ showSolid: on }); // also just shows/hides it
  if (on && state.viewer) await state.viewer.ensureSolid(); // fetch if needed
};
$("f-time").onchange = loadFrame;
$("f-refresh").onclick = () => { loadFrame(); loadForces(); };

initForm();
connectWS();
window.__viewer = null; // handy for debugging; set when created
