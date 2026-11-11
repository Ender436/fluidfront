# Engine: FluidX3D (pinned clean clone)

Pinned at tag **v3.8** (detached HEAD, see `engine/pin.txt`). This folder is
gitignored; a fresh clone is recreated with `make engine` (clone-if-missing),
so upstream updates can be fetched deliberately; every build uses our build
of this tree, never a system install.

Build (headless, no X11) - Fluidfront picks the right one per OS:

    make Linux -j$(nproc)                       # Linux  -> bin/FluidX3D
    MSBuild FluidX3D.vcxproj /p:Configuration=Release /p:Platform=x64
                                                # Windows -> bin\FluidX3D.exe

## How Fluidfront drives the engine

The app never patches the solver core. A case is entirely expressed through
two **generated** files (see `fluidfront/gen/`):

- `src/setup.cpp` — generated from `case.json`: creates the `LBM` object,
  geometry (procedural primitives or `voxelize_stl` for STL models),
  equilibrium-boundary tunnel faces, the measurement loop
  (`object_force` -> `forces.csv`), and VTK snapshots
  (`write_device_to_vtk`, SI units).
- `src/defines.hpp` — regenerated from the pristine upstream file
  (`git show HEAD:src/defines.hpp`) with only these switches: velocity set
  (D3Q19/D2Q9), precision (FP32/FP16S/FP16C), SRT, VOLUME_FORCE,
  FORCE_FIELD, EQUILIBRIUM_BOUNDARIES, SUBGRID. Everything else stays as
  upstream.

After generation both files also live under `<case>/gen/` for
reproducibility. `make Linux` rebuilds; `bin/FluidX3D` runs with the case
folder as cwd and writes into `<case>/output/`.

Notes:
- OpenCL headers + a stub `libOpenCL.so` are vendored in `src/OpenCL/`; the
  real driver (`libOpenCL.so.1`) is loaded dynamically at runtime. No system
  OpenCL dev packages needed.
- One shared build tree: cases build serially (fine for a local app; a
  per-case build dir is possible later if we ever parallelize the queue).
- `fluidfront/validate/runner.py` is the tripwire for any engine drift or
  bridge bug: it re-runs physical validations (cylinder Re=100 Cd/St,
  Poiseuille pipe) through the real pipeline. Run it after every engine
  change: `make check`.

License: FluidX3D is source-available for non-commercial use (LICENSE.md in
this folder). This propagates to the combined application.
