"""Loader for FluidX3D v3.8 binary VTK exports.

Format written by `Memory_Container::write_vtk` (lbm.hpp): STRUCTURED_POINTS,
big-endian. Data is cell-major (AoS): the d components of each cell sit
together, and the flat cell index runs x-fastest. Used by the server to
stream snapshots into the WebUI and by the validator.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_DTYPE = {"FLOAT": ">f4", "DOUBLE": ">f8", "UNSIGNED_CHAR": ">u1",
          "INT": ">i4", "CHAR": ">i1", "SHORT": ">i2", "UNSIGNED_SHORT": ">u2"}


def load_vtk(path: Path | str) -> dict:
    path = Path(path)
    with open(path, "rb") as f:
        lines = []
        for _ in range(9):  # fixed number of ASCII header lines in v3.8 writer
            lines.append(f.readline().decode("ascii").strip())
        if lines[0] != "# vtk DataFile Version 3.0":
            raise ValueError(f"{path}: not a FluidX3D v3.8 VTK file")
        if lines[2] != "BINARY" or lines[3] != "DATASET STRUCTURED_POINTS":
            raise ValueError(f"{path}: expected BINARY STRUCTURED_POINTS")
        nx, ny, nz = (int(v) for v in lines[4].split()[1:])
        origin = [float(v) for v in lines[5].split()[1:]]
        spacing = [float(v) for v in lines[6].split()[1:]]
        npts = int(lines[7].split()[1])
        parts = lines[8].split()  # SCALARS data TYPE ncomp
        dtype, ncomp = _DTYPE[parts[2].upper()], int(parts[3]) if len(parts) > 3 else 1
        f.readline()  # LOOKUP_TABLE default
        raw = np.frombuffer(f.read(), dtype=dtype)
    if raw.size != npts * ncomp:
        raise ValueError(f"{path}: truncated data ({raw.size} != {npts*ncomp})")
    field = np.moveaxis(raw.reshape(nz, ny, nx, ncomp), -1, 0)  # AoS -> (ncomp, nz, ny, nx)
    return {"dims": (nx, ny, nz), "origin": origin, "spacing": spacing,
            "ncomp": ncomp, "field": field, "t": _t_from_name(path)}


def _t_from_name(path: Path) -> int:
    try:
        return int(path.stem.split("-")[-1])
    except ValueError:
        return -1
