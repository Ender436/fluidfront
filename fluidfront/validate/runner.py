"""Validation suite: the trust layer of Fluidfront.

Runs real end-to-end cases through the exact pipeline the app uses
(case.json -> generated setup.cpp -> compiled FluidX3D -> forces.csv +
VTK snapshots) and compares against analytic / literature values.

    python -m fluidfront.validate.runner [--only NAME] [--fast]

Cases (default):
  cyl2d_re100    2D cylinder, Re=100:  Cd = 1.35 +/- (voxel tolerance),
                 Strouhal ~ 0.165 (Bark et al. 1999: Cd~1.37, St~0.164)
  poiseuille_3d  pressure-driven round pipe, exact parabolic profile:
                 L2 error < 5% (Krüger p.256: LBM typically 2-5%)
  sphere3d_re100 STL model (generated sphere) voxelized on GPU, Re=100:
                 Cd ~ 1.09 (Schiller-Naumann) within voxel/BC tolerance
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from fluidfront.gen.build import BuildError, build, write_case_files
from fluidfront.gen.runner import run_case
from fluidfront.gen.schema import load_case
from fluidfront.gen.units import CaseError
from fluidfront.vtkio import load_vtk

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "cases" / "_validate"

# ---------------------------------------------------------------------------
# case definitions

CYL2D = {
    "name": "cyl2d_re100",
    "domain": {"nx": 384, "ny": 1024, "nz": 1},
    "geometry": {"kind": "cylinder", "size_lu": 32, "center": [0.5, 0.25, 0.5]},
    "fluid": {"kind": "air"},
    # air 20C: nu=1.516e-5 -> Re = 1.516 m/s * 1 mm / nu = 100 exactly
    "flow": {"mode": "tunnel", "u_si": 1.516, "length_si": 1.0e-3, "u_lu": 0.1},
    "options": {"velocity_set": "d2q9", "precision": "fp32"},
    "run": {"t_steps": 32000, "force_every": 20, "snapshot_every": 8000},
}

POISEUILLE = {
    "name": "poiseuille_3d",
    "domain": {"nx": 128, "ny": 256, "nz": 128},
    "geometry": {"kind": "pipe", "size_lu": 126},  # radius 63 (upstream validation)
    "fluid": {"kind": "air"},
    "flow": {"mode": "volume_force", "u_max_lu": 0.1, "tau": 1.0},
    "options": {"velocity_set": "d3q19", "precision": "fp32"},
    "run": {"t_steps": 30000, "force_every": 50, "snapshot_every": 10000},
}

CASES = [(CYL2D, "check_cyl2d"), (POISEUILLE, "check_poiseuille")]

# --- M2: STL round-trip (trimesh -> binary STL -> GPU voxelizer) ----------
SPHERE3D = {
    "name": "sphere3d_re100",
    "domain": {"nx": 192, "ny": 448, "nz": 192},
    "geometry": {"kind": "stl", "path": "model.stl", "size_lu": 48,
                 "center": [0.5, 0.3, 0.5], "rotation_deg": [0.0, 0.0, 0.0]},
    "fluid": {"kind": "air"},
    # 1 m sphere, Re=100 -> u = 100*nu/D = 1.516e-3 m/s
    "flow": {"mode": "tunnel", "u_si": 1.516e-3, "length_si": 1.0, "u_lu": 0.1},
    "options": {"velocity_set": "d3q19", "precision": "fp16s"},
    "run": {"t_steps": 16000, "force_every": 25, "snapshot_every": 1000000},
}
CASES.append((SPHERE3D, "check_sphere3d"))


_PREPARES: dict[str, object] = {}  # case name -> prep callback (explicit
                                   # registry; name-matching was fragile)


def prepare_sphere3d(case_dir: Path) -> None:
    """Build the STL model with the real geometry pipeline (M2 coverage):
    trimesh sphere -> loaders cleanup -> binary STL on disk."""
    from fluidfront.geometry.loaders import export_binary_stl
    import trimesh

    mesh = trimesh.creation.icosphere(subdivisions=3, radius=0.5)  # 1 m dia
    export_binary_stl(mesh, case_dir / "model.stl")


_PREPARES[SPHERE3D["name"]] = prepare_sphere3d


# ---------------------------------------------------------------------------
# measurements from the case outputs

def read_forces(case_dir: Path) -> np.ndarray:
    path = case_dir / "output" / "forces.csv"
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}")
    return np.loadtxt(path, delimiter=",", comments="#")


def check_cyl2d(case_dir: Path, spec) -> tuple[bool, str]:
    data = read_forces(case_dir)
    t, cd, cl = data[:, 0], data[:, 4], data[:, 5]

    tail = slice(int(0.6 * len(t)), None)  # last 40% after wake is established
    cd_mean = float(np.mean(cd[tail]))

    # Strouhal from lift autocorrelation
    sig = cl[tail] - np.mean(cl[tail])
    n = len(sig)
    acf = np.correlate(sig, sig, mode="full")[n - 1:]
    neg = np.flatnonzero(acf < 0.0)
    if len(neg) == 0 or neg[0] + 2 >= n:
        return False, f"no oscillation found in lift signal (Cd={cd_mean:.3f})"
    lag = int(np.argmax(acf[neg[0]:])) + int(neg[0])   # period in samples
    period_steps = lag * int(spec.run.get("force_every", 20))
    d_lu, u_lu = spec.geometry["size_lu"], spec.derived.u_lu
    st = d_lu / (u_lu * period_steps)

    ok_cd = 1.25 <= cd_mean <= 1.47
    ok_st = 0.145 <= st <= 0.185
    return (ok_cd and ok_st), (
        f"Cd = {cd_mean:.3f} (want 1.25..1.47, ref 1.35-1.37), "
        f"St = {st:.3f} (want 0.145..0.185, ref ~0.165), "
        f"Re_lu = {spec.derived.re_lu:.1f}")


def check_poiseuille(case_dir: Path, spec) -> tuple[bool, str]:
    files = sorted((case_dir / "output").glob("u-*.vtk"))
    if not files:
        return False, "no velocity snapshots found"
    snap = load_vtk(files[-1])
    nx, ny, nz = snap["dims"]
    u = snap["field"]  # (3, nz, ny, nx)
    y = ny // 2
    umax = float(spec.flow.get("u_max_lu", 0.1))
    R = 0.5 * float(spec.geometry["size_lu"])

    xs = np.arange(nx) + 0.5 - 0.5 * nx
    zs = np.arange(nz) + 0.5 - 0.5 * nz
    r = np.sqrt(xs[None, :] ** 2 + zs[:, None] ** 2)
    inside = r < R
    unum = np.sqrt(u[0, :, y, :] ** 2 + u[1, :, y, :] ** 2 + u[2, :, y, :] ** 2)
    uref = np.where(inside, umax * (R ** 2 - np.minimum(r, R) ** 2) / R ** 2, 0.0)

    err = float(np.sqrt(np.sum((unum[inside] - uref[inside]) ** 2)
                        / np.sum(uref[inside] ** 2)))
    ok = err < 0.05
    return ok, f"parabolic-profile L2 error = {100 * err:.2f}% (want < 5%), t={snap['t']}"


def check_sphere3d(case_dir: Path, spec) -> tuple[bool, str]:
    data = read_forces(case_dir)
    t, cd = data[:, 0], data[:, 4]
    tail = slice(int(0.55 * len(t)), None)  # settled wake (last ~45%)
    cd_mean = float(np.mean(cd[tail]))
    cd_std = float(np.std(cd[tail]))
    # Schiller-Naumann at Re=100: Cd = 24/Re (1 + 0.15 Re^0.687) ~= 1.08;
    # allow for voxelization + equilibrium-BC blockage bias (~ +/- 15%)
    ok = 0.95 <= cd_mean <= 1.25
    return ok, (f"Cd = {cd_mean:.3f} +/- {cd_std:.3f} (want 0.95..1.25, "
                f"ref 1.08), Re_lu = {spec.derived.re_lu:.1f}, "
                f"projected-area-based")


# ---------------------------------------------------------------------------

def _event_printer(state: dict):
    def on_event(ev: dict) -> None:
        if ev["kind"] == "progress":
            if ev["percent"] >= state["next_pct"]:
                state["next_pct"] += 20
                print(f"    [{ev['percent']:3d}%] {ev['mlups']} MLUPs, eta {ev['eta']}",
                      flush=True)
        elif ev.get("line", "").startswith("fluidfront:"):
            print("    " + ev["line"], flush=True)
    return on_event


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=[c["name"] for c, _ in CASES])
    args = ap.parse_args(argv)

    failures = 0
    for case_dict, checker_name in CASES:
        name = case_dict["name"]
        if args.only and args.only != name:
            continue
        print(f"\n=== {name} ===", flush=True)
        cdir = OUT / name
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "case.json").write_text(json.dumps(case_dict, indent=2))
        prepare = _PREPARES.get(name)
        if prepare:
            prepare(cdir)
        checker = globals()[checker_name]
        try:
            spec = load_case(cdir)
            for w in spec.derived.warnings:
                print(f"  warn: {w}")
            t0 = time.time()
            write_case_files(spec)
            binary = build()
            print("  built engine for this case")
            rc = run_case(binary, cdir, _event_printer({"next_pct": 20}))
            if rc != 0:
                raise CaseError(f"engine exited with code {rc} (see {cdir}/run.log)")
            ok, msg = checker(cdir, spec)
            print(f"  {'PASS' if ok else 'FAIL'}: {msg}  [{time.time() - t0:.0f}s]")
            failures += 0 if ok else 1
        except (CaseError, BuildError, FileNotFoundError) as e:
            print(f"  FAIL: {e}")
            failures += 1

    print(f"\nvalidate: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
