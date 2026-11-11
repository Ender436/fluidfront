# Fluidfront — intuitive GPU CFD for hobbyists (PLAN, decisions locked)

> A FluidX3D-style tool with a real user interface: drop in a 3D model
> (STL/OBJ/GLB), pick a condition ("wind tunnel at 60 m/s", quality level),
> watch a beautiful live 3D flow, get honest drag numbers — no meshing, no
> config files, no CFD knowledge required.

## 0. Decisions (locked Nov 2026)

**Execution constraints (user rules, always in force):**
- File edits only inside the project directory or a temp directory
  (`/tmp/opencode`); anything elsewhere → ask first.
- No system-package installs or dev-environment changes that leak outside
  this project → everything project-local: Python `venv` in `.venv/`, npm in
  `web/node_modules/`, engine pinned in `engine/`. If a genuine system
  dependency appears (e.g. an OpenCL driver component), ask before installing.

| Question | Decision |
|---|---|
| Primary use cases | **External aerodynamics** — model rockets, cars, planes. Secondary: **fans / indoor flow**. |
| Commercial | Not a goal. Hobby project for hobbyists. Leaning on other OSS is explicitly OK where the license allows. |
| Solver engine | **FluidX3D v3.8 (forked, driven by generated setup files)** — see §2. Its non-commercial license is acceptable; our own layers stay MIT. |
| Hardware | Main machine: desktop **RTX 3090 (24 GB)** — VRAM is not a constraint. Dev machine: this laptop (RTX 3050 Ti, 4 GB) for building/testing small grids. FluidX3D is OpenCL → runs identically on both. |
| UI | **Localhost WebUI** (Vite + three.js) talking to a local Python server. |

## 1. Why wrapping FluidX3D instead of writing our own solver (changed from first draft)

Their FAQ's benchmark table (verified from the README, Nov 2026):
- **RTX 3090: 5,418 MLUPs/s (FP32)** and **10,732 MLUPs/s (FP32/FP16S)** —
  ~10-30× what a from-scratch Python/Warp core would realistically reach.
- Memory: 93 B/cell (FP32) → **~260 M cells ≈ 640³**; 55 B/cell (FP16S) →
  **~450 M cells ≈ 770³** in 24 GB.
- Practical: 256³ case settles in **well under a minute**, 512³ turbulent wake
  case in **minutes**. "Detailed" quality can genuinely mean 512³–640³.

It also ships, already validated, the things that would be months to build:
GPU STL voxelization, interpolated bounce-back boundaries, KBC entropic +
Smagorinsky stabilization for high Re, force measurement (momentum exchange),
VTK export, checkpointing, thermal module, and it runs on OpenCL (both our
GPUs, and any hobbyist's AMD/Intel too). License: source-available, free for
non-commercial use — fine for this project's stated goals.

**What remains original (where the actual user value lives):** everything in
front of the solver — the wizard, case files, geometry placement,
auto-parameterization, live field streaming, the three.js viewer, force charts,
export, and the "guard rails" that replace CFD expertise.

## 2. Architecture

```
┌──────────────────────────────┐       ┌─────────────────────────────────┐
│ WebUI (localhost)            │  WS   │ App server (Python/FastAPI)     │
│ Vite + three.js + TS         │◄─────►│ case manager, job runner,       │
│ - import & placement gizmo   │  HTTP │ case.json → setup.cpp generator,│
│ - live field renderer        └──────►│ snapshot/force tailer, exports  │
│ - charts, results, galleries │       │            │ spawns             │
└──────────────────────────────┘       │            ▼                    │
                                       │ FluidX3D fork (OpenCL, uncored  │
                                       │ modifications): compiled from   │
                                       │ generated setup.cpp per case;   │
                                       │ writes snapshot VTK/PNG +       │
                                       │ forces.csv in a loop            │
                                       └─────────────────────────────────┘
```

Engine contract (uses only FluidX3D's documented extension points — no core
hacks):
- **Case → solver:** `case.json` (geometry path/transform, fluid, speed,
  quality, preset) → generated `setup.cpp` from templates → compile
  (~5-30 s via their make.sh machinery) → run binary with device pick.
- **Live data:** the generated setup runs in a loop: `lbm.run(N)` → dump
  downsampled field snapshot (binary VTK) + append force row via
  `measure_force()` → server tails these files and streams to the browser.
- **Geometry:** any format (OBJ/GLB/STL) → trimesh → watertight check →
  binary STL → `lbm.voxelize_stl(path, center, rotation, size)`; the wizard's
  placement handles produce exactly those arguments.

## 3. The product: 4 screens (unchanged from first draft)

1. **Import** — drag&drop model → 3D viewer with move/scale/rotate handles;
   auto-fitted tunnel domain with padding.
2. **Conditions** — Case preset: *Wind tunnel* (rocket/car/plane), *Room +
   fan*, *Pipe*; fluid (**Air / Water / Custom**), speed in real units,
   **Quality** (Preview / Standard / Detailed → grid size). Reynolds, lattice
   units, τ, stabilization: computed silently, shown as info.
3. **Run** — live 3D (speed/pressure/vorticity, slices, tracers/streamlines,
   wake iso-surfaces), live Cd/Cl chart, "flow settled ✓", pause/stop.
4. **Results** — Cd/Cl with confidence note (grid-convergence based), key
   visuals, one-click MP4/GIF + VTK (ParaView) export, case folder to share.

Guard rails: resolution warnings ("fin is 3 voxels thick — use Detailed"),
inlet ramp + sponge layers, blow-up detector with auto-rollback/retune,
quality capped where physics wouldn't be trustworthy anyway.

## 4. Milestones (each ends runnable + tested)

**M0 — Scaffold (½ day).** Repo layout (§6), Python venv (fastapi, trimesh,
numpy, pyevtk), Vite app stub, `fluidfront serve`. Clone + build FluidX3D v3.8
on the laptop; run its built-in benchmark → first MLUPs/VRAM calibration.

**M1 — Engine bridge (biggest de-risk).** FluidX3D fork with a template
`setup.cpp` + `case.json` → generator round-trip: run a cylinder-in-tunnel
case end-to-end; snapshots + forces.csv appear; replay viewer reads them.
**Validation:** Taylor–Green and channel flow quick checks, then cylinder at
Re=100 → Cd ≈ 1.35±0.05, St ≈ 0.165 vs literature. This suite runs on every
code change after; it's the trust layer.

**M2 — Geometry pipeline.** trimesh loader (STL/OBJ/GLB→binary STL),
watertight check + repair hints, bbox measure, auto domain fit + padding per
preset, transform handles → (center, rotation, size) args. Voxel-quality
estimates (min feature size in voxels) → warnings.

**M3 — Presets + auto-parameterization (the "no CFD knowledge" brain).**
Wind-tunnel BC template (Zou/He ramped inlet, convective outlet + sponge,
slip sides); Re-based speed→lattice mapping with Ma guard; KBC on above Re
threshold; rocket-specific defaults (slender-body domain aspect, spin off);
fan preset v1 (actuator-disk momentum region inside the generated setup).
Force per object, settled-flow detector, time estimates from M0 calibration,
checkpoint/restore, stability auto-recovery.

**M4 — App server.** Case CRUD on disk (`case.json`, `geometry.*`,
`checkpoints/`, `snapshots/`, `forces.csv`), start/pause/stop, WebSocket
status + downsampled field streaming (throttled), VTK export, device/VRAM
budget model.

**M5 — WebUI wizard + live viewer (the "wow" milestone).** Four screens from
§3. Viewer: slice planes + particles + surface coloring, colormap switcher,
orbit camera. Cd/Cl live chart. Run library with thumbnails.

**M6 — Visualization polish.** Q-criterion wake iso-surfaces, cinematic
off-screen render pass → ffmpeg MP4/GIF, results report page, share-case.

**M7 — Extras (as desired).** FP16S storage build flag for 700³+ grids;
thermal preset (conjugate heat transfer is in FluidX3D already); room/vent
presets v2 (proper fan curves, porous media); multi-GPU; nicer dam-break /
free-surface fun cases.

## 5. Risks & mitigations

- **setup.cpp template fragility / API drift on FluidX3D updates** → pin fork
  at v3.8; M1 validation suite is the tripwire; keep generator + templates
  small and fully covered by the M1 cases.
- **Live-view granularity** (snapshot cadence) → adaptive snapshot interval
  scaled to grid size; interpolation/decimation client-side; FluidX3D's own
  PNG renderer remains available as a fallback display.
- **High Re (rockets, cars) on coarse grids** → KBC/Smagorinsky built into
  presets; confidence notes state the error class; "Detailed" tier pushes to
  512³+ where the 3090 makes it minutes.
- **Thin features voxelizing badly** → min-feature warnings (M2), upsell to
  Detailed quality; Cd computed against *voxelized* projected area so numbers
  stay self-consistent.
- **Scope creep into engineering CFD** → README scope: fast, qualitative-to-
  semi-quantitative design tool; VTK export is the escape hatch to ParaView.
- **Licensing** → our server/UI = MIT; the bundled FluidX3D fork keeps its
  non-commercial license, so the combined app must stay non-commercial. Fine
  per §0; revisit if that ever changes (fallback: clean-room Warp core).

## 6. Repo layout

```
project1/
├── PLAN.md / README.md / LICENSE (MIT, our code)
├── Makefile                    # serve, check, bench
├── fluidfront/
│   ├── gen/         # case.json schema, setup.cpp templates, generator
│   ├── geometry/    # loaders.py, watertight.py, autolayout.py
│   ├── presets/     # wind_tunnel.py, room_fan.py, pipe.py, materials.py
│   ├── server/      # app.py (FastAPI), jobs.py, streaming.py, exports.py
│   └── validate/    # cylinder_re100.py, taylor_green.py, channel.py, runner
├── engine/FluidX3D/ # pinned fork (v3.8) + our templates/hooks
├── web/             # Vite + three.js + TS (viewer/, wizard/, api/)
├── cases/           # user cases (gitignored)
└── stl/             # sample models (rocket, car, sphere, cylinder…)
```

## 7. Definition of "v0 works" (the demo that proves the whole idea)

On the 3090: drop a model-rocket STL → Wind tunnel / Air / 60 m/s / Standard
→ within ~5 min: settled flow, live 3D wake visualization, Cd curve, one-click
MP4. On the laptop: same flow at Preview/Standard-small so development and
testing work everywhere.

## 8. Progress log

- **2026-11-09 — M0 done.** Scaffold, `.venv`, FastAPI serve test, FluidX3D
  v3.8 pinned (tag v3.8 = `50150a7`; nested clone gitignored, `make engine`
  clones if missing, pin in `engine/pin.txt`), headless `make Linux` build OK
  with vendored OpenCL headers. Laptop calibration in BENCH.md: RTX 3050 Ti
  = 2318 MLUPs (256³ FP16S), 984 MiB.
- **2026-11-09 — M1 done.** Generator (`case.json` → `setup.cpp` +
  `defines.hpp`, token templating) + build/run/progress-parse + binary-VTK
  loader. `make check` passes end-to-end through the real pipeline:
  - `cyl2d_re100` (D2Q9, D=32LU, exact Re via nu_lu=0.032): **Cd = 1.349**
    (lit. 1.35–1.37), **St = 0.172** (lit. ~0.165) → exact-Re mapping and
    `object_force` measurement validated.
  - `poiseuille_3d` (pipe R=63, tau=1, volume force): parabolic-profile
    **L2 error 0.18%** (want <5%) → voxelized walls + SI-free snapshots OK.
  Gotchas learned: VTK writer appends the field name itself (`OUT` must end
  in `/`, else `uu-*.vtk`); data layout is AoS cell-major big-endian, x
  fastest; float literals need `32.0f` not `32f`.
- **2026-11-09 — M2 done.** Geometry pipeline (`fluidfront/geometry/`):
  loaders (STL/OBJ/GLB via trimesh -> merged/cleaned **binary** STL),
  quality checks with human hints (watertight/open-edge count, normals,
  low-poly), auto-layout (tunnel fit: 4x-width/8x-length rules, blockage
  auto-widen, VRAM-cap -> resolution downshift), quality tiers
  preview/standard/detailed = 48/96/192 voxels on the model's longest edge.
  Wizard backend `geometry.pipeline.build_tunnel_case()` -> complete case.json.
  New validation `sphere3d_re100`: trimesh icosphere STL -> GPU voxelizer ->
  **Cd = 1.233** (ref 1.08 Schiller-Naumann; voxel-area + confinement bias,
  steady wake, within tolerance). New hard guard rail: tau < 0.512 -> refuse
  with actionable advice ("raise quality / slow down"), LES auto-on 0.512-0.545.
  `make check` = cyl2d + poiseuille + sphere all PASS.
- **2026-11-09 — renamed flowbench → fluidfront.** "flowbench" is taken on
  PyPI; rename would only get more expensive over time. Package, imports,
  Makefile, docs all updated; validation re-run under the new name (green).
- **Next: M4** app server (user chose to build the app skeleton before M3's
  solver-refinement presets, which will grow inside real usage).
- **2026-11-10 — M4 skeleton done.** `fluidfront/server/`: FastAPI app +
  `jobs.JobManager` (serial run queue on the shared engine build tree, worker
  thread build->run, stop via cancel-event -> engine process terminate,
  status.json persisted per case, WebSocket fan-out). Endpoints: device
  budget (nvidia-smi -> max_cells), case create from model upload
  (multipart -> geometry pipeline -> case.json + report.json; 422 carries the
  human guard-rail text), list/detail/delete, run/stop, forces, snapshots,
  downsampled field (base64 float32), `/ws` events. Smoke-tested end-to-end
  on the laptop: upload sphere STL -> 96x192x96 Re=53 case -> run (live
  progress on WS) -> forces + field fetch -> stop at 67% -> done.
  Fixed along the way: `schema.load_case` now WRITES BACK normalized
  force_every/snapshot_every (previously the alignment fix was computed but
  unused -> snapshots never fired: `t % 1000` never hit a multiple of 18);
  snapshots now also always fire at the final step. Runner got `cancel`
  event support for stop.
- **Next: M5** WebUI wizard/viewer against this API.
- **2026-11-10 — M5 done (WebUI skeleton).** `web/` = plain static ES modules
  served by FastAPI from the same origin (no Vite/npm build step, no CORS);
  three.js r170 vendored under `web/vendor/` (MIT, committed) so it runs
  offline. Four screens (Cases / Import+Conditions / Run / Results) wired to
  the API: drag-drop model upload -> guard-rail report (Re, tau, blockage,
  cells, estimated run time); Run screen driven purely by WS status events;
  Results = three.js GPU point-cloud viewer (velocity magnitude or density,
  turbo/viridis/gray colormaps, slow-cell threshold, point size, solid model
  rendered from the flags snapshot, orbit camera, snapshot time selector)
  + hand-rolled canvas Cd/Cl chart with tail-mean line, a +/- spread and a
  "wake still oscillating" honesty warning. Browser-tested end-to-end:
  created the "model" case through the wizard (sphere, preview tier,
  size_lu 24), watched building->running->done live, viewed t=7500, switched
  to rho (new: pipeline now sets `write_rho: true` on new cases). Fixed the
  model-solid flag mask: v3.8 enum is TYPE_S=1, TYPE_E=2, TYPE_X=64, so body
  cells are 65, not 48 (old mask was v2 numbering). Noted for M3: the 24-LU
  preview sphere reports a stuck negative Cd, and a 48-LU rerun oscillates
  (Cd 1.7 +/- 3.4) - coarse-resolution artifact; the UI now shows spread +
  warning instead of one confident-looking average.
- **Next: M6** visualization polish (iso-surfaces, slice planes, MP4 export);
  revisit coarse-resolution guard rails (M3) informed by the note above.
- **2026-11-10 — real-world model fixes (user testing with the ME180 kart STL).**
  Three failure modes found and fixed:
  1. 500 Internal Server Error on upload: `trimesh.edge_face_count` does not
     exist in trimesh 5.2 -> replaced with a version-proof boundary-edge count
     (`np.unique` over sorted edge pairs; caught a reshape-before-sort bug in
     the first version too). create_case now has a catch-all -> clean 422.
  2. The kart STL is in centimeters, but the app assumed meters -> Re 79M and
     a guard-rail refusal. Added Model units (m/cm/mm/in) to the wizard +
     "model reads 240 m long" sanity warning + model length shown in report.
  3. tau guard rail: "pick a higher quality" advice was mathematically useless
     at real speeds (max resolvable Re grows only linearly with size_lu; the
     2.4m kart at 5 m/s is Re 790k - unreachable at any tier). Replaced the
     hard refusal with an LES clamp at the measured stability floor tau=0.515:
     run Smagorinsky at the lowest stable viscosity and report the effective
     resolved Re honestly. Exact-Re mapping for resolvable cases is unchanged
     (validated sphere Re=100 tau=0.788 still passes).
  Also: VRAM budget now also respects the driver single-OpenCL-buffer cap
  (~1/4 VRAM; measured 955 MB on the 3050 Ti - this, not total VRAM, was why
  a 56M-cell kart case died at allocation) and time estimates derate ~1.6x
  for LES. End-to-end proof on the 4 GB laptop: Full_Kart.stl as cm, 5 m/s,
  24.3M cells, size_lu 61, tau 0.515 + SUBGRID, done in ~3.5 min wall,
  Cd = 1.10 +/- 0.39 (wake oscillation warning fires - honest as designed).
- **Viewer rotation fix** — the results viewer kept showing the first model
  it ever loaded (`load()` only built the solid when `!this.solid`, and one
  Viewer instance is shared across cases), so changing rotation_deg appeared
  to do nothing even though the engine flags were correctly rotated (proved:
  full_kart body extent 59x98x39 voxels vs 0,0,90-rotated 46x28x19). Now
  `load()` disposes the cached solid on case switch (proper geometry/material
  dispose, no more `viewer.solid = null` leak-hack), re-frames the camera per
  case, and a tiny GET /api/cases/{name}/stamp (setup.cpp mtime) fingerprint
  rebuilds the solid if a case was re-created. Solid toggle now just shows/
  hides + fetches on demand instead of refetching the whole field. Verified
  in-browser: three kart cases with different rotations each render their own
  orientation, toggle on/off clean, 0 console errors. Also fixed case cards
  showing "undefined": WS status events have `case` but no `name` field.
- **Wake view (hide fast flow)** — new "hide fast flow" checkbox on the results
  viewer: velocity points faster than X% of the inflow vanish, leaving only
  the flow the model slowed down (wake, recirculation, wall boundary layers),
  recolored across the full colormap so wake structure pops. The "fast"
  reference is the field's MEDIAN speed - key discovery: the engine's VTK
  snapshots carry physical units (kart case: freestream ~12 m/s, not 0.1 LU),
  so the median self-calibrates regardless of units; feeding u_lu=0.1 as the
  reference had hidden 99.8% of the field. Slider label is now contextual:
  "hide slow (<X%)" for velocity, "keep wake (<X% inflow)" in wake mode,
  "hide calm" for density. Verified on full_kart: 205k points plain vs
  1.8k/3.5k/6.5k at 30/60/90% wake cutoffs, toggle back restores fast mode.
- **Published.** Public repo live at github.com/Ender436/fluidfront (MIT for
  app code; FluidX3D stays a gitignored pinned clone fetched by `make engine`
  - its non-commercial terms documented in LICENSE + README).
- **Quit semantics hardened** — Ctrl+C on the server now cleanly stops a
  running sim (lifespan shutdown -> cancel -> engine terminated; verified:
  child gone, status 'stopped', port free ~9s with an open websocket; note
  uvicorn waits for live WS before app-shutdown). Plus prctl(PR_SET_PDEATHSIG)
  on the engine child so even a FORCE quit (double Ctrl+C) or a server crash
  can never orphan a GPU-hogging FluidX3D (verified by force-quit test).
- **Native Windows support** (user picked native over WSL2 so no dual-boot
  switching): build.py is OS-aware - Windows path locates MSBuild via vswhere
  and builds the upstream FluidX3D.vcxproj (Release|x64 -> bin\FluidX3D.exe;
  OpenCL headers+import lib are vendored in the engine, so the only prereq is
  VS 2022 Build Tools C++ workload). runner.py: prctl guarded to posix,
  Windows gets a Job Object with KILL_ON_CLOSE (same no-orphan guarantee);
  stdout pipe uses errors="replace". README + engine/README document the
  PowerShell flow. Linux regression: rebuilt engine + full create/run/quit
  smoke passed. Windows side is UNTESTED (no Windows machine here) - expect
  debug round-trips on the first desktop session.
- **Windows first-run fix** (user's desktop test): MSVC died with C2513 on
  generated setup.cpp line 13 - our variable `OUT` collides with the Windows
  SDK's `#define OUT` (SAL annotations via Windows.h, included by utilities.hpp);
  g++ never sees that macro, hence Linux-only bug. Renamed to PREFIX (verified
  repro with `g++ -DOUT=`; Linux rebuild + run smoke green). Rest of MSVC
  build was clean - only lodepng warnings.
- **Windows status-lock fix** (user's desktop test #2): runs stalled/failed with
  WinError 5 renaming status.json.tmp -> status.json. Windows refuses to rename
  over a file another handle has open (the UI's status polling) - atomic-rename
  idiom is Linux-only. One transient hiccup had killed the worker thread mid-run
  AND orphaned the solver on the GPU. Fixes: (1) live status kept in memory,
  file only for restart persistence, our own readers never touch it mid-write;
  (2) persist = tmp+rename, on OSError fall back to direct write, never raise;
  (3) run_case now try/finally-terminates the engine on ANY loop exception,
  and progress-handler exceptions are logged, not fatal. Unit-tested the
  WinError-5 simulation on Linux (patched Path.replace to throw) + real run
  with exploding handler -> engine reaped, no orphan; full Linux smoke green.
- **Next: M6** visualization polish; also run the full flow on the desktop 3090
  (clone there + `make engine setup serve`; bigger buffer cap -> higher tiers
  become usable, and its BENCH numbers are worth re-measuring live).
