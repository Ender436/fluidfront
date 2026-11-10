# Flowbench

Intuitive GPU CFD for hobbyists: drop in a 3D model, pick a condition, watch
the flow. No meshing, no config files, no CFD expertise.

- **Solver engine:** [FluidX3D](https://github.com/ProjectPhysX/FluidX3D)
  (lattice Boltzmann, OpenCL, runs on any GPU) — pinned fork in `engine/`.
- **App:** Python server (`flowbench/`) + local WebUI (`web/`, three.js).
- **License:** our code MIT; the bundled engine fork keeps FluidX3D's
  non-commercial terms → the combined app must stay non-commercial.

See **PLAN.md** for the full plan, milestones, and execution constraints.

## Quick start (development, on any machine with a GPU)

```
make engine   # build the FluidX3D engine (headless)
make setup    # create .venv and install python deps
make serve    # start the app server on http://localhost:8123
make bench    # run the solver benchmark (calibration numbers)
make check    # validation suite (trust layer)
```

## Layout

| Path | What |
|---|---|
| `flowbench/` | app server, case generator, geometry, presets, validation |
| `engine/FluidX3D/` | pinned solver fork (see `engine/README.md`) |
| `web/` | WebUI (Vite + three.js) — scaffolded in M5 |
| `cases/` | user case folders (gitignored) |
| `stl/` | sample models (gitignored) |
