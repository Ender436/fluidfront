"""case.json schema: parse, validate, and derive the resolved case spec.

A case is fully described by a small JSON file (kept inside the case
folder for reproducibility). The generator turns it into a FluidX3D
`setup.cpp` + `defines.hpp`; nothing else about the engine is patched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .units import CaseError, Derived, derive

GEOMETRY_KINDS = {"cylinder", "sphere", "cuboid", "pipe", "stl", "none"}
PRECISIONS = {"fp32", "fp16s", "fp16c"}
VELOCITY_SETS = {"d3q19", "d2q9"}


@dataclass
class CaseSpec:
    name: str
    case_dir: Path
    raw: dict              # original case.json contents
    derived: Derived

    # resolved shortcuts used by the generator
    nx: int
    ny: int
    nz: int
    geometry: dict
    flow: dict
    run: dict
    precision: str
    velocity_set: str
    subgrid: bool
    subgrid_model: str
    output_dir: Path


def _check_domain(dom: dict) -> tuple[int, int, int]:
    try:
        nx, ny, nz = int(dom["nx"]), int(dom["ny"]), int(dom["nz"])
    except (KeyError, TypeError, ValueError) as e:
        raise CaseError(f"domain needs integer nx/ny/nz: {e}")
    for n, nm in ((nx, "nx"), (ny, "ny"), (nz, "nz")):
        if n < 8 and not (nm == "nz" and n == 1):
            raise CaseError(f"{nm}={n} too small (min 8; nz may be 1 for 2D)")
    # keep dimensions multiples of 4 (vectorization / halo friendliness)
    if any(n % 4 for n in (nx, ny)) or (nz != 1 and nz % 4):
        raise CaseError("nx/ny must be multiples of 4, nz a multiple of 4 or 1 "
                        "(round them in the app before submitting)")
    return nx, ny, nz


def load_case(case_dir: Path) -> CaseSpec:
    import json

    case_dir = Path(case_dir).resolve()
    path = case_dir / "case.json"
    if not path.is_file():
        raise CaseError(f"no case.json in {case_dir}")
    raw = json.loads(path.read_text())

    for key in ("domain", "geometry", "flow", "run"):
        if key not in raw:
            raise CaseError(f"case.json missing required section {key!r}")

    name = str(raw.get("name", case_dir.name))
    nx, ny, nz = _check_domain(raw["domain"])

    geom = raw["geometry"]
    kind = str(geom.get("kind", "none")).lower()
    if kind not in GEOMETRY_KINDS:
        raise CaseError(f"geometry.kind {kind!r} not in {sorted(GEOMETRY_KINDS)}")
    if kind == "stl":
        stl = Path(geom.get("path", ""))
        if not stl.is_absolute():
            stl = (case_dir / stl).resolve()
        if not stl.is_file():
            raise CaseError(f"STL not found: {geom.get('path')!r}")
        if stl.read_bytes()[:5] == b"solid":
            raise CaseError("ASCII STL not supported by the engine: convert "
                            "to binary STL (the app does this for you)")
        geom["path_abs"] = str(stl)
    geom.setdefault("size_lu", 0.25 * min(nx, ny, nz))

    flow = raw["flow"]
    mode = str(flow.get("mode", "tunnel")).lower()
    if mode not in ("tunnel", "volume_force"):
        raise CaseError(f"flow.mode {mode!r} unknown")

    derived = derive(raw)

    opts = raw.get("options", {})
    precision = str(opts.get("precision", "fp32")).lower()
    if precision not in PRECISIONS:
        raise CaseError(f"options.precision {precision!r} not in {sorted(PRECISIONS)}")
    vsel = str(opts.get("velocity_set", "d3q19")).lower()
    if vsel not in VELOCITY_SETS:
        raise CaseError(f"options.velocity_set {vsel!r} unknown")
    if vsel == "d2q9" and nz != 1:
        raise CaseError("d2q9 requires nz == 1")
    if nz == 1 and vsel != "d2q9":
        raise CaseError("nz == 1 (2D case) requires velocity_set d2q9")
    subgrid_model = str(opts.get("subgrid_model", "smagorinsky")).lower()
    if subgrid_model != "smagorinsky":
        # The pinned FluidX3D v3.8 tree only ships the Smagorinsky-Lilly LES
        # kernel; accepting other names here would silently run a different
        # model than the case file claims.
        raise CaseError(f"options.subgrid_model {subgrid_model!r} is not "
                        "available in the pinned engine (use 'smagorinsky')")
    if "smagorinsky_const" in opts:
        # the pinned engine bakes the Smagorinsky-Lilly constant into its
        # kernel (C = 1/pi*(2/(3*CK))^(3/4) ~= 0.173); we never patch the
        # engine, so honouring a custom value is impossible - reject it
        # instead of silently ignoring it
        raise CaseError("options.smagorinsky_const is not configurable in the "
                        "pinned engine (fixed Smagorinsky-Lilly constant ~= "
                        "0.173); remove the option")
    subgrid = bool(opts.get("subgrid", derived.subgrid_suggested))
    if derived.les_clamped and "subgrid" in opts and not opts["subgrid"]:
        raise CaseError(
            "this case sits at the solver's minimum viscosity (LES clamp): it "
            "cannot run with the subgrid model turned off")

    run = raw["run"]
    force_every = int(run.get("force_every", 25))
    if force_every < 1:
        raise CaseError("run.force_every must be >= 1")
    snap_every = int(run.get("snapshot_every", max(5 * force_every, 100)))
    if snap_every < 1:
        raise CaseError("run.snapshot_every must be >= 1")
    if snap_every % force_every:
        snap_every = ((snap_every // force_every) + 1) * force_every
    # write normalized values back: the generator must use THESE, or snapshot
    # times never align with the force-write loop (t is only ever a multiple
    # of force_every) and no field snapshots get written at all
    run["force_every"] = force_every
    run["snapshot_every"] = snap_every
    if int(run.get("t_steps", 0)) < 1:
        raise CaseError("run.t_steps must be >= 1")

    out = case_dir / "output"
    out.mkdir(parents=True, exist_ok=True)

    return CaseSpec(
        name=name, case_dir=case_dir, raw=raw, derived=derived,
        nx=nx, ny=ny, nz=nz, geometry=geom, flow=flow, run=run,
        precision=precision, velocity_set=vsel, subgrid=subgrid,
        subgrid_model=subgrid_model,
        output_dir=out,
    )
