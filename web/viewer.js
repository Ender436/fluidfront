// Live flow viewer: velocity/density fields as a GPU point cloud, the solid
// model rendered from the engine's flags snapshot, and Q-criterion vortex
// iso-surfaces. Data comes from GET /api/cases/{name}/field and /q
// (downsampled float32 on the server).

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { MarchingCubes } from "three/addons/objects/MarchingCubes.js";

// ---- colormaps (same 5-stop idea as common scientific maps) --------------
const CMAPS = {
  turbo: [[0.13, 0.05, 0.5], [0.24, 0.65, 0.95], [0.3, 0.93, 0.55],
          [0.95, 0.75, 0.1], [0.85, 0.09, 0.14]],
  viridis: [[0.27, 0.0, 0.33], [0.13, 0.57, 0.77], [0.9, 0.98, 0.3]],
  gray: [[0.05, 0.06, 0.08], [1, 1, 1]],
};

function ramp(cmap, t) {
  const stops = CMAPS[cmap] || CMAPS.turbo;
  const x = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  const f = x - i;
  return [stops[i][0] + (stops[i + 1][0] - stops[i][0]) * f,
          stops[i][1] + (stops[i + 1][1] - stops[i][1]) * f,
          stops[i][2] + (stops[i + 1][2] - stops[i][2]) * f];
}

// View-detail budgets: how many points/voxels/Q-grid cells the browser asks
// the server for. The simulation physics is fixed at run time; this only
// controls how much of it the view shows (a detailed sim has more real detail
// to show once the budget is raised).
const DETAIL = {
  fast: { points: 100_000, solid: 500_000, qgrid: 64 },
  balanced: { points: 300_000, solid: 2_000_000, qgrid: 96 },
  sharp: { points: 1_000_000, solid: 4_000_000, qgrid: 128 },
};

async function getField(name, which, t, maxPoints) {
  const p = new URLSearchParams({ which, max_points: String(maxPoints),
                                  fmt: "bin" });
  if (t !== null && t !== undefined) p.set("t", String(t));
  const r = await fetch(`/api/cases/${name}/field?${p}`);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  const meta = JSON.parse(r.headers.get("X-Meta"));
  return { dims: meta.dims, ncomp: meta.ncomp, t: meta.t,
           data: new Float32Array(await r.arrayBuffer()) };
}

async function getStamp(name) {
  const r = await fetch(`/api/cases/${name}/stamp`);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return (await r.json()).stamp;
}

async function getQ(name, t, grid) {
  const p = new URLSearchParams({ grid: String(grid || 96), fmt: "bin" });
  if (t !== null && t !== undefined) p.set("t", String(t));
  const r = await fetch(`/api/cases/${name}/q?${p}`);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  const meta = JSON.parse(r.headers.get("X-Meta"));
  return { q: new Float32Array(await r.arrayBuffer()), dims: meta.dims,
           t: meta.t, full_dims: meta.full_dims,
           iso_lo: meta.iso_lo, iso_hi: meta.iso_hi };
}

export class Viewer {
  constructor(container) {
    this.box = container;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0b0e13);
    this.camera = new THREE.PerspectiveCamera(55, 1, 0.01, 100);
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    container.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    // flow runs along +x now: look at the model from front-left-above
    this.camera.position.set(0.7, 0.5, 0.9);
    this.controls.target.set(0.33, 0.25, 0.25);
    this._framed = false;

    this.points = null;
    this.solid = null;
    this.iso = null; // MarchingCubes vortex surface (field === "q")
    this.qmeta = null; // {iso_lo, iso_hi} of the loaded Q grid
    this.stamp = null; // server setup fingerprint of the loaded solid
    this.settings = { field: "u", cmap: "turbo", thresh: 0.05,
                      pointSize: 3, showSolid: true, wake: false,
                      sliceAxis: "", slicePos: 0.5, detail: "balanced" };
    this._raf = null;
    this._resize();
    new ResizeObserver(() => this._resize()).observe(container);
    this._loop = this._loop.bind(this);
    this._loop();
  }

  _resize() {
    const w = this.box.clientWidth || 640, h = this.box.clientHeight || 480;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  }

  _loop() {
    this._raf = requestAnimationFrame(this._loop);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  _clearMeshes() {
    for (const obj of [this.points, this.solid, this.iso]) {
      if (!obj) continue;
      this.scene.remove(obj);
      obj.geometry.dispose();
      obj.material.dispose();
    }
    this.points = this.solid = this.iso = null;
  }

  _disposeSolid() {
    if (!this.solid) return;
    this.scene.remove(this.solid);
    this.solid.geometry.dispose();
    this.solid.material.dispose();
    this.solid = null;
    this.stamp = null;
  }

  setSettings(patch) {
    Object.assign(this.settings, patch);
    this.refresh(); // recolors without re-fetching
  }

  // Load flags (model) + field for `name` at snapshot time <= t.
  async load(name, t = null) {
    if (this.caseName !== name) {
      // different case: drop its stale model solid + re-frame the camera
      this._disposeSolid();
      this._framed = false;
      this.caseName = name;
    }
    this.lastT = t;
    const budget = DETAIL[this.settings.detail] || DETAIL.balanced;
    if (this.settings.showSolid) await this.ensureSolid();
    if (this.settings.field === "q") {
      if (this.points) { // Q mode: drop the point cloud
        this.scene.remove(this.points);
        this.points.geometry.dispose();
        this.points.material.dispose();
        this.points = null;
      }
      const fq = await getQ(name, t, budget.qgrid);
      return this._buildIso(fq);
    }
    this._disposeIso();
    const f = await getField(name, this.settings.field, t, budget.points);
    this._buildPoints(f);
    this.frameInfo = `t=${f.t} · ${f.dims.join("×")}`;
    return this.frameInfo;
  }

  // Fetch (or rebuild) the model solid if it is missing or the case was
  // re-created with new settings — rotation/quality are baked into the flags.
  async ensureSolid() {
    if (!this.caseName) return;
    try {
      const stamp = await getStamp(this.caseName);
      if (this.solid && stamp === this.stamp) {
        this.solid.visible = true;
        return;
      }
      const fl = await getField(this.caseName, "flags", this.lastT,
                                (DETAIL[this.settings.detail] || DETAIL.balanced).solid);
      this._disposeSolid();
      this._buildSolid(fl);
      this.stamp = stamp;
    } catch (e) { console.warn("flags load:", e.message); }
  }

  refresh() {
    if (this.settings.field === "q" && this.iso) {
      this.iso.isolation = this._isoLevel(); // threshold slider: re-polygonize
      this.iso.update();                     // (no refetch)
      return;
    }
    if (this.raw) this._buildPoints(this.raw); // recolor with new settings
  }

  // engine coords (x=width, y=flow, z=height) -> three.js with flow along
  // +x and engine height up; divide by the LARGEST dim so nothing is stretched
  _pt(x, y, z, d) {
    const s = Math.max(d.nx, d.ny, d.nz);
    return [y / s, z / s, x / s];
  }

  // engine field arrays are [c, z, y, x] nested: index = ((c*nz+z)*ny+y)*nx+x
  // (the server already downsamples to fit max_points, so walk every cell)
  _buildSolid(fl) {
    const [nx, ny, nz] = fl.dims;
    const d = { nx, ny, nz };
    const flags = fl.data; // stored float32 but values are small ints
    const pos = [];
    for (let i = 0, z = 0; z < nz; z++) {
      for (let y = 0; y < ny; y++) {
        for (let x = 0; x < nx; x++, i++) {
          const v = flags[i];
          if ((v & 65) === 65) { // TYPE_S(1) | TYPE_X(64) = model body
            pos.push(...this._pt(x, y, z, d));
          }
        }
      }
    }
    if (!pos.length) return;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    const m = new THREE.PointsMaterial({ color: 0x8b98a9, size: 0.008,
                                         sizeAttenuation: true });
    this.solid = new THREE.Points(g, m);
    this.scene.add(this.solid);
    this._syncUnits();
  }

  _buildPoints(f) {
    const [nx, ny, nz] = f.dims, nc = f.ncomp, d = f.data;
    const dim = { nx, ny, nz };
    const s = this.settings;
    if (this.points) {
      this.scene.remove(this.points);
      this.points.geometry.dispose();
      this.points.material.dispose();
      this.points = null;
    }
    const count = nx * ny * nz;
    const pos = new Float32Array(count * 3);
    const col = new Float32Array(count * 3);
    let w = 0;
    let vmax = 1e-12;
    const mag = new Float32Array(count);
    // scalar fields (rho) are offset: SI-scaled ~1.2 in air; view deviations
    let base = 0;
    if (nc < 3) {
      let sum = 0;
      for (let i = 0; i < count; i++) sum += d[i];
      base = sum / count;
    }
    for (let i = 0; i < count; i++) {
      let v;
      if (nc >= 3) {
        const x = d[i], y = d[count + i], z = d[2 * count + i];
        v = Math.sqrt(x * x + y * y + z * z);
      } else {
        v = Math.abs(d[i] - base);
      }
      mag[i] = v;
      if (v > vmax) vmax = v;
    }
    // velocity: "hide fast flow" keeps only what the model slowed down, and
    // recolors across the wake so its structure stands out; "fast" mode and
    // density keep the original peak-relative thresholding. "fast" reference =
    // the field's median speed: in a tunnel most cells ARE the undisturbed
    // inflow, and snapshots are in physical units, so this self-calibrates.
    const isVel = nc >= 3;
    const wake = isVel && s.wake;
    let uRef = vmax;
    if (wake) {
      const samp = [];
      for (let i = 0; i < count; i += 7) samp.push(mag[i]);
      samp.sort((a, b) => a - b);
      uRef = samp[samp.length >> 1] || vmax;
    }
    const cut = wake ? Math.max(1e-9, s.thresh * uRef) : 0;
    // slice plane: keep a thin band of cells around one coordinate plane
    const lim = s.sliceAxis === "x" ? nx : s.sliceAxis === "y" ? ny
      : s.sliceAxis === "z" ? nz : -1;
    const cutIdx = lim < 0 ? -1 : Math.min(lim - 1, Math.round(s.slicePos * (lim - 1)));
    for (let z = 0; z < nz; z++) {
      for (let y = 0; y < ny; y++) {
        for (let x = 0; x < nx; x++) {
          if (cutIdx >= 0) {
            const c = s.sliceAxis === "x" ? x : s.sliceAxis === "y" ? y : z;
            if (Math.abs(c - cutIdx) > 1) continue;
          }
          const i = (z * ny + y) * nx + x;
          let t; // colormap position 0..1
          if (wake) {
            if (mag[i] >= cut) continue;
            t = mag[i] / cut;
          } else {
            if (mag[i] <= s.thresh * vmax) continue;
            t = mag[i] / vmax;
          }
          const [r, g, b] = ramp(s.cmap, t);
          const p = this._pt(x, y, z, dim);
          pos[w * 3] = p[0]; pos[w * 3 + 1] = p[1]; pos[w * 3 + 2] = p[2];
          col[w * 3] = r; col[w * 3 + 1] = g; col[w * 3 + 2] = b;
          w++;
        }
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos.subarray(0, w * 3), 3));
    g.setAttribute("color", new THREE.BufferAttribute(col.subarray(0, w * 3), 3));
    const m = new THREE.PointsMaterial({ size: s.pointSize * 0.004,
      vertexColors: true, sizeAttenuation: true, transparent: true,
      opacity: 0.9, depthWrite: false });
    this.points = new THREE.Points(g, m);
    this.scene.add(this.points);
    this.raw = f;
    this._syncUnits();
  }

  _disposeIso() {
    if (!this.iso) return;
    this.scene.remove(this.iso);
    this.iso.geometry.dispose();
    this.iso.material.dispose();
    this.iso = null;
  }

  // Q-criterion iso-surface from the server's uniform Q grid (see
  // fluidfront/qcriterion.py). MarchingCubes' field layout (x fastest, then
  // y, z) matches the server array exactly, so we can bulk-load it.
  _buildIso(fq) {
    this._disposeIso();
    this.isoDims = fq.full_dims;
    this.qmeta = fq;
    if (!(fq.iso_hi > 0)) { // honest: nothing resolved as swirling in this frame
      this.frameInfo = `t=${fq.t} · no resolved vortices in this frame`;
      return this.frameInfo;
    }
    const N = fq.dims[0];
    const mc = new MarchingCubes(N,
      new THREE.MeshNormalMaterial({ side: THREE.DoubleSide }),
      false, false, 40000); // constructor calls init(N) itself - do not re-init
    mc.field.set(fq.q);
    mc.isolation = this._isoLevel();
    mc.update();
    // engine axes -> three.js (same cyclic map as _pt): local x->world z,
    // local y->world x, local z->world y = -120 deg about (1,1,1)
    mc.setRotationFromAxisAngle(new THREE.Vector3(1, 1, 1).normalize(),
                                -2 * Math.PI / 3);
    const [nx, ny, nz] = fq.full_dims, s = Math.max(nx, ny, nz);
    mc.scale.set(nx / (2 * s), ny / (2 * s), nz / (2 * s));
    mc.position.set(ny / (2 * s), nz / (2 * s), nx / (2 * s));
    this.iso = mc;
    this.scene.add(mc);
    this._syncUnits();
    this.frameInfo = `t=${fq.t} · Q iso ${Math.round(this.isoValue)} 1/s²`;
    return this.frameInfo;
  }

  // threshold slider (0..0.9) -> log scale between the frame's typical (iso_lo,
  // geometric mean of positive Q) and max vortex strength (iso_hi)
  _isoLevel() {
    const m = this.qmeta;
    const k = Math.min(1, Math.max(0, this.settings.thresh / 0.9));
    this.isoValue = m.iso_lo * Math.pow(m.iso_hi / m.iso_lo, k);
    return this.isoValue;
  }

  _syncUnits() {
    // aim at the model (engine center: x=0.5·nx, y≈0.33·ny, z=0.5·nz)
    const dims = this.raw?.dims || this.isoDims;
    if (dims) {
      const [nx, ny, nz] = dims, s = Math.max(nx, ny, nz);
      this.controls.target.set(0.33 * ny / s, 0.5 * nz / s, 0.5 * nx / s);
      if (!this._framed) {
        this.camera.position.set(0.33 * ny / s + 0.45, 0.5 * nz / s + 0.35,
                                 0.5 * nx / s + 0.75);
        this._framed = true;
      }
    }
    if (this.solid) this.solid.visible = this.settings.showSolid;
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this._clearMeshes();
    this.renderer.dispose();
    this.box.innerHTML = "";
  }
}
