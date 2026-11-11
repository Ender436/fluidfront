# Fluidfront

Intuitive GPU CFD for hobbyists: drop in a 3D model, pick a condition, watch
the flow. No meshing, no config files, no CFD expertise.

- **Solver engine:** [FluidX3D](https://github.com/ProjectPhysX/FluidX3D)
  (lattice Boltzmann, OpenCL, runs on any GPU) — pinned clean clone, fetched
  by `make engine` (not redistributed here).
- **App:** Python server (`fluidfront/`) + local WebUI (`web/`, three.js).
- **License:** our code MIT; the engine keeps FluidX3D's non-commercial
  terms → the combined app must stay non-commercial.

See **PLAN.md** for the full plan, milestones, and execution constraints.

## Quick start (development, on any machine with a GPU)

```
make engine   # build the FluidX3D engine (headless)
make setup    # create .venv and install python deps
make serve    # start the app server on http://localhost:8123
make bench    # run the solver benchmark (calibration numbers)
make check    # validation suite (trust layer)
```

Then open **http://localhost:8123** — the wizard (import a model, pick a
speed, run, watch the live 3D viewer) is served from the same port.

## Windows (native, no reboot / no WSL)

Prerequisites (one-time):
- [Python 3.11+](https://python.org) and [Git for Windows](https://git-scm.com)
- **Visual Studio 2022 Build Tools** with the *Desktop development with C++*
  workload (this compiles the engine; the OpenCL headers/lib FluidX3D needs
  are vendored, so nothing else to install) — https://visualstudio.microsoft.com/downloads/
- normal NVIDIA/AMD/Intel GPU **drivers** (they include the OpenCL runtime)

In PowerShell:

```powershell
git clone https://github.com/Ender436/fluidfront.git
cd fluidfront
git clone --branch v3.8 --depth 1 https://github.com/ProjectPhysX/FluidX3D.git engine/FluidX3D
py -m venv .venv
.venv\Scripts\activate
pip install -e .
python -m uvicorn fluidfront.server.app:app --port 8123
```

Open **http://localhost:8123**. The first simulation run compiles the engine
automatically (MSBuild, ~1–2 min); afterwards everything behaves exactly like
on Linux, including clean Ctrl+C shutdown (a running sim is killed with the
server via a Windows Job Object, so the GPU is never left busy). Windows
Defender may show a firewall prompt on first start — allowing it is only
needed if you want the UI from another device; same-machine use works either
way.

## API preview (M4 — the WebUI will use exactly this)

```
curl -F "model=@my_model.stl" -F "u_si=12" -F "tier=preview" localhost:8123/api/cases
curl -X POST localhost:8123/api/cases/my-model/run
# live progress:  ws://localhost:8123/ws
# results:        /api/cases/my-model/forces  and  /api/cases/my-model/field
```

## Layout

| Path | What |
|---|---|
| `fluidfront/` | app server, case generator, geometry, presets, validation |
| `engine/FluidX3D/` | pinned clean clone of the solver (see `engine/README.md`) |
| `web/` | WebUI (plain ES modules + vendored three.js, no build step) |
| `cases/` | user case folders (gitignored) |
| `stl/` | sample models (gitignored) |

## Third-party software & licensing

- **FluidX3D** (solver engine, ProjectPhysX): source-available but
  **non-commercial**; NOT included in this repo - `make engine` clones the
  pinned upstream tag into `engine/FluidX3D/` (gitignored). The combined app
  must therefore stay non-commercial. See `LICENSE` and `engine/pin.txt`.
- **three.js** (MIT): vendored under `web/vendor/` so the UI runs offline.
- Everything else in this repository: MIT (see `LICENSE`).
