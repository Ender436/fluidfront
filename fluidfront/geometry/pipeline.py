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


def build_tunnel_case(model_path: Path | str, case_dir: Path | str, *,
                      u_si: float, tier: str = "standard",
                      fluid: dict | str = "air",
                      rotation_deg=DEFAULT_ROTATION,
                      size_lu: int | None = None,
                      max_cells: int | None = None,
                      precision: str = "fp16s") -> tuple[dict, dict]:
    """Returns (case_dict, report). Writes `<case_dir>/model.stl`.
    size_lu: explicit voxels on the model's longest edge (overrides tier).

    report: {'warnings': [...], 'stats': {...}, 'layout': Layout}
    """
    case_dir = Path(case_dir)
    mesh = load_model(model_path)

    insp = inspect_mesh(mesh)
    rot = tuple(float(v) for v in rotation_deg)
    bb = bbox_rotated(mesh, rot)
    lay = fit_tunnel(bb["dims"], tier=tier, size_lu=size_lu, max_cells=max_cells)

    stl_path = export_binary_stl(mesh, case_dir / "model.stl")

    warnings = [f"{text} ({hint})" for sev, text, hint in insp["problems"]
                if sev != "hint"]
    warnings += feature_warnings(bb["dims"] / bb["max_dim"] * lay.size_lu,
                                 lay.size_lu)
    warnings += lay.notes

    # physical reference length: the longest bbox edge, which is exactly the
    # extent scaled to `size_lu` voxels -> keeps units.py consistent
    length_si = bb["max_dim"]

    ny = lay.ny
    u_lu = 0.1
    t_steps = int(round(4.0 * ny / u_lu / 500.0) * 500)  # ~4 flow-throughs
    force_every = min(50, max(10, t_steps // 400))

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
                "snapshot_every": max(1000, t_steps // 8)},
    }
    report = {"warnings": warnings, "stats": insp["stats"], "layout": lay,
              "blockage": lay.blockage}
    return case, report
