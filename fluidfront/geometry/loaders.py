"""Model loading: STL/OBJ/GLB (and anything trimesh reads) -> watertight binary STL.

The engine voxelizes *binary* STL only, scales the model so that the longest
edge of its (post-rotation) bounding box equals `size_lu`, and places the box
center at `center`. Everything upstream of that is handled here:
- merge multi-node scenes (GLB assemblies) into a single mesh
- numeric cleanup (duplicate vertices, degenerate faces)
- rotation-aware bbox measurement so the app can show real clearances
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import trimesh

SUPPORTED_IMPORT = {".stl", ".obj", ".glb", ".gltf", ".ply", ".3mf", ".dae"}


class GeometryError(Exception):
    pass


def load_model(path: Path | str) -> trimesh.Trimesh:
    """Load any supported model file as a single Trimesh."""
    path = Path(path)
    if not path.is_file():
        raise GeometryError(f"model file not found: {path}")
    if path.suffix.lower() not in SUPPORTED_IMPORT:
        raise GeometryError(
            f"{path.suffix or 'no extension'} not supported "
            f"(use one of {sorted(SUPPORTED_IMPORT)})")
    try:
        obj = trimesh.load(str(path), force="mesh")
    except Exception as e:  # noqa: BLE001 - surface as app-level error
        raise GeometryError(f"could not read {path.name}: {e}") from e
    if not isinstance(obj, trimesh.Trimesh) or len(obj.faces) == 0:
        raise GeometryError(f"{path.name}: no triangulated surface found")

    obj.merge_vertices()
    obj.update_faces(obj.nondegenerate_faces())
    obj.remove_unreferenced_vertices()
    if len(obj.faces) == 0:
        raise GeometryError(f"{path.name}: nothing left after cleanup")
    return obj


def export_binary_stl(mesh: trimesh.Trimesh, out_path: Path | str) -> Path:
    """Write the mesh as binary STL (what the engine reads)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(out_path))
    if out_path.read_bytes()[:5] == b"solid":  # trimesh writes binary, but be safe
        raise GeometryError("STL written as ASCII; refusing (engine needs binary)")
    return out_path


def rotation_matrix(rotation_deg) -> np.ndarray:
    """Engine's euler convention: R = Rx * Ry * Rz (extrinsic XYZ, degrees)."""
    rx, ry, rz = (math.radians(float(v)) for v in rotation_deg)

    def rot(axis, a):
        c, s = math.cos(a), math.sin(a)
        x, y, z = axis
        return np.array([
            [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
            [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
            [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
        ])

    return rot((1, 0, 0), rx) @ rot((0, 1, 0), ry) @ rot((0, 0, 1), rz)


def bbox_rotated(mesh: trimesh.Trimesh, rotation_deg=(0.0, 0.0, 0.0)) -> dict:
    """Axis-aligned bbox of the rotated mesh, in model units."""
    R = rotation_matrix(rotation_deg)
    v = mesh.vertices @ R.T
    lo, hi = v.min(axis=0), v.max(axis=0)
    dims = hi - lo
    return {
        "dims": dims,                       # (dx, dy, dz)
        "max_dim": float(dims.max()),
        "center_model": (lo + hi) / 2.0,    # engine re-centers on this
        "min": lo, "max": hi,
    }
