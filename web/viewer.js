// Live flow viewer: velocity/density fields as a GPU point cloud plus the
// solid model rendered from the engine's flags snapshot. Data comes from
// GET /api/cases/{name}/field (downsampled float32 on the server).

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

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

async function getField(name, which, t, maxPoints) {
  const p = new URLSearchParams({ which, max_points: String(maxPoints) });
  if (t !== null && t !== undefined) p.set("t", String(t));
  const r = await fetch(`/api/cases/${name}/field?${p}`);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  const j = await r.json();
  const bin = atob(j.data_b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return { dims: j.dims, ncomp: j.ncomp, t: j.t, stamp: j.stamp,
           data: new Float32Array(buf.buffer) };
}

async function getStamp(name) {
  const r = await fetch(`/api/cases/${name}/stamp`);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return (await r.json()).stamp;
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
    this.stamp = null; // server setup fingerprint of the loaded solid
    this.settings = { field: "u", cmap: "turbo", thresh: 0.05,
                      pointSize: 3, showSolid: true };
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
    for (const obj of [this.points, this.solid]) {
      if (!obj) continue;
      this.scene.remove(obj);
      obj.geometry.dispose();
      obj.material.dispose();
    }
    this.points = this.solid = null;
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
    if (this.settings.showSolid) await this.ensureSolid();
    const f = await getField(name, this.settings.field, t, 200000);
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
      const fl = await getField(this.caseName, "flags", this.lastT, 2000000);
      this._disposeSolid();
      this._buildSolid(fl);
      this.stamp = stamp;
    } catch (e) { console.warn("flags load:", e.message); }
  }

  refresh() {
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
    for (let z = 0; z < nz; z++) {
      for (let y = 0; y < ny; y++) {
        for (let x = 0; x < nx; x++) {
          const i = (z * ny + y) * nx + x;
          if (mag[i] <= s.thresh * vmax) continue;
          const [r, g, b] = ramp(s.cmap, mag[i] / vmax);
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

  _syncUnits() {
    // aim at the model (engine center: x=0.5·nx, y≈0.33·ny, z=0.5·nz)
    const dims = this.raw?.dims;
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
