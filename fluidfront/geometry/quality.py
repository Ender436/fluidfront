"""Mesh quality checks that translate into human advice, not CFD jargon.

What voxelization actually punishes: open surfaces (non-watertight meshes
voxelize unpredictably), degenerate/inverted normals, and features thinner
than a few voxels. Each finding gets a plain-language hint the wizard shows.
"""

from __future__ import annotations

import numpy as np
import trimesh

# features thinner than this (voxels) get "will not survive voxelization" hints
MIN_FEATURE_VOXELS_WARN = 4.0
MIN_FEATURE_VOXELS_FAIL = 2.0


def boundary_edge_count(mesh: trimesh.Trimesh) -> int:
    """Edges shared by exactly one face (openings in the shell). Version-proof:
    trimesh's private edge-count helpers move around between releases."""
    pairs = np.sort(mesh.faces[:, [0, 1, 1, 2, 2, 0]], axis=1).reshape(-1, 2)
    _, counts = np.unique(pairs, axis=0, return_counts=True)
    return int((counts == 1).sum())


def inspect_mesh(mesh: trimesh.Trimesh) -> dict:
    """Return {'ok': bool, 'problems': [(severity, text, hint)], 'stats': {...}}."""
    problems: list[tuple[str, str, str]] = []

    if not mesh.is_watertight:
        hints = []
        if hasattr(mesh, "is_winding_consistent") and not mesh.is_winding_consistent:
            hints.append("some faces point inwards (flipped normals)")
        holes = boundary_edge_count(mesh)
        problems.append((
            "warn",
            "the model has open surfaces (not watertight)",
            f"the voxelizer needs a closed shell{f' (~{holes} open edges found)'}; "
            "try the built-in auto-repair, or close holes in Blender "
            "(select all in Edit Mode -> Mesh > Cleanup > Make Manifold)",
        ))
    if hasattr(mesh, "is_winding_consistent") and not mesh.is_winding_consistent:
        problems.append((
            "warn",
            "face orientations are inconsistent",
            "run auto-repair (recalculates normals)",
        ))

    stats = {
        "triangles": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "watertight": bool(mesh.is_watertight),
        "extent": [float(v) for v in (mesh.bounds[1] - mesh.bounds[0])],
    }
    if stats["triangles"] < 500:
        problems.append((
            "hint",
            "very low triangle count",
            "export a higher-poly version; voxel quality follows mesh quality",
        ))
    ok = not any(sev == "fail" for sev, _, _ in problems)
    return {"ok": ok, "problems": problems, "stats": stats}


def feature_warnings(bbox_dims_lu, size_lu: float) -> list[str]:
    """Warn based on model bbox dimensions in voxels at the chosen quality.

    Honest and cheap: it flags thin overall envelopes (fins, wings, blades)
    which voxelize badly; per-feature analysis is a later refinement.
    """
    out = []
    for name, d in zip(("width", "length", "height"), bbox_dims_lu):
        if d < MIN_FEATURE_VOXELS_FAIL:
            out.append(f"model {name} is only {d:.0f} voxels at this quality - "
                       "results will not be trustworthy; raise quality")
        elif d < MIN_FEATURE_VOXELS_WARN:
            out.append(f"model {name} is just {d:.0f} voxels - "
                       "consider a higher quality setting")
    return out
