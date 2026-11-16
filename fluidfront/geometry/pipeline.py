"""One-call wizard backend: model file + a few human choices -> complete case.json.

This composes the geometry pipeline (load -> inspect -> measure) with the
auto-layout and unit mapping, and emits exactly the schema understood by
gen/schema.py. The M4 server will call `build_tunnel_case()` when the user
finishes the Conditions screen.
"""

from __future__ import annotations

from pathlib import Path

from .autolayout import fit_tunnel
from .loaders import bbox_rotated, export_binary_stl, load_model
from .quality import feature_warnings, inspect_mesh

DEFAULT_ROTATION = (90.0, 0.0, 0.0)  # STL Z-up -> tunnel: length along +y flow


def read_mesh(model_path: Path | str) -> tuple:
    """Load + inspect + measure a model without building a case: what the
    Import screen wants to show (tri-count, watertight, size in meters)."""
    mesh = load_model(model_path)
    insp = inspect_mesh(mesh)
    bb = bbox_rotated(mesh, DEFAULT_ROTATION)
    return insp, bb


def build_tunnel_case(model_path: Path | str, case_dir: Path | str, *,
                      u_si: float, tier: str = "standard",
                      fluid: dict | str = "air",
                      rotation_deg=DEFAULT_ROTATION,
                      size_lu: int | None = None,
                      max_cells: int | None = None,
                      scale_si: float = 1.0,
                      precision: str = "fp16s",
                      throughs: float = 4.0) -> tuple[dict, dict]:
    """Returns (case_dict, report). Writes `<case_dir>/model.stl`.
    size_lu: explicit voxels on the model's longest edge (overrides tier).
    scale_si: meters per model unit (CAD exports are often mm/cm - the
    Reynolds number is meaningless if the app guesses the scale wrong).

    report: {'warnings': [...], 'stats': {...}, 'layout': Layout}
    """
    case_dir = Path(case_dir)
    model_path = Path(model_path)
    mesh = load_model(model_path)

    insp = inspect_mesh(mesh)
    rot = tuple(float(v) for v in rotation_deg)
    bb = bbox_rotated(mesh, rot)
    lay = fit_tunnel(bb["dims"], tier=tier, size_lu=size_lu, max_cells=max_cells)

    stl_target = case_dir / "model.stl"
    if model_path.resolve() == stl_target.resolve():
        stl_path = stl_target  # already the processed model.stl (resized case)
    else:
        stl_path = export_binary_stl(mesh, stl_target)

    warnings = [f"{text} ({hint})" for sev, text, hint in insp["problems"]
                if sev != "hint"]
    if scale_si == 1.0 and bb["max_dim"] > 4.0:
        warnings.append(
            f"model reads {bb['max_dim']:.1f} m long - if that is wrong, your "
            "CAD file is probably in cm or mm: set the Model units option")
    warnings += feature_warnings(bb["dims"] / bb["max_dim"] * lay.size_lu,
                                 lay.size_lu)
    warnings += lay.notes

    # physical reference length: the longest bbox edge, which is exactly the
    # extent scaled to `size_lu` voxels -> keeps units.py consistent
    length_si = bb["max_dim"] * scale_si

    ny = lay.ny
    u_lu = 0.1
    t_steps = int(round(throughs * ny / u_lu / 500.0) * 500)  # `throughs` passes
    force_every = min(50, max(10, t_steps // 400))
    # field snapshots double as the video/timeline frames, so their count is
    # visible to users: aim for one per quarter flow-through (8..32 total),
    # but cap disk use - each snapshot VTK stores u (3f) + rho (f) ~16 B/cell
    cells = lay.nx * lay.ny * lay.nz
    want_snap = max(8, min(4 * round(throughs), 32))
    disk_snap = max(8, int(6e9 // max(1, cells * 16)))
    n_snap = min(want_snap, disk_snap)
    if n_snap < want_snap:
        warnings.append(
            f"big grid: timeline/video frames capped at {n_snap} to keep "
            f"snapshot files under ~{n_snap * cells * 16 / 1e9:.0f} GB")
    # snapshots must land on force-write steps (t is only ever a multiple of
    # force_every) or the engine never writes a field snapshot
    snapshot_every = max(100, t_steps // n_snap)
    snapshot_every = -(-snapshot_every // force_every) * force_every

    case = {
        "name": case_dir.name,
        "domain": lay.as_domain(),
        "geometry": {
            "kind": "stl",
            "path": stl_path.name,  # case-relative: case.json stays portable
            "size_lu": lay.size_lu,
            "center": list(lay.center),
            "rotation_deg": list(rot),
        },
        "fluid": {"kind": fluid} if isinstance(fluid, str) else dict(fluid),
        "flow": {"mode": "tunnel", "u_si": float(u_si),
                 "length_si": float(length_si), "u_lu": u_lu},
        "options": {"velocity_set": "d3q19", "precision": precision},
        "run": {"t_steps": t_steps, "force_every": force_every,
                "snapshot_every": snapshot_every, "write_rho": True},
    }
    report = {"warnings": warnings, "stats": insp["stats"], "layout": lay,
              "blockage": lay.blockage,
              # actual timeline/video frame count (engine also writes the
              # final step, so a remainder adds one more)
              "frames": t_steps // snapshot_every + bool(t_steps % snapshot_every)}
    return case, report
