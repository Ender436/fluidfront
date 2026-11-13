"""Streamline tracing through a snapshot velocity grid.

For a steady flow the streamlines are just the integral curves of the
velocity direction field, so timing plays no role: every seed line is
marched in lockstep with RK2 along the *normalized* velocity in fixed
cell-size steps. A line ends at a solid cell, the domain boundary, or a
stagnation point. Feeds GET /api/cases/{name}/streamlines; the viewer
draws the polylines as segments colored by |u|.
"""

from __future__ import annotations

import math

import numpy as np

BODY_MASK = 65        # engine TYPE_S(1) | TYPE_X(64) = model voxels
_STEP = 0.75          # integration step in grid cells
_EPS = 1e-9           # "not moving" threshold (SI m/s)


def _sample(u_flat: np.ndarray, nx: int, ny: int, nz: int,
            pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Trilinear-interpolate velocity at pos (N,3) in cell coords.

    Returns ((3,N) float32, inside-mask). Out-of-domain rows are flagged,
    their values are garbage but never used."""
    x0 = np.floor(pos[:, 0]).astype(np.int32)
    y0 = np.floor(pos[:, 1]).astype(np.int32)
    z0 = np.floor(pos[:, 2]).astype(np.int32)
    inside = ((x0 >= 0) & (x0 + 1 < nx) & (y0 >= 0) & (y0 + 1 < ny)
              & (z0 >= 0) & (z0 + 1 < nz))
    xc0 = np.clip(x0, 0, nx - 1)
    yc0 = np.clip(y0, 0, ny - 1)
    zc0 = np.clip(z0, 0, nz - 1)
    xc1 = np.clip(x0 + 1, 0, nx - 1)
    yc1 = np.clip(y0 + 1, 0, ny - 1)
    zc1 = np.clip(z0 + 1, 0, nz - 1)
    fx = (pos[:, 0] - x0).astype(np.float32)
    fy = (pos[:, 1] - y0).astype(np.float32)
    fz = (pos[:, 2] - z0).astype(np.float32)

    def flat(zc, yc, xc):
        return u_flat[:, (zc * ny + yc) * nx + xc]

    vel = np.zeros((3, pos.shape[0]), np.float32)
    for zc, wz in ((zc0, 1 - fz), (zc1, fz)):
        for yc, wy in ((yc0, 1 - fy), (yc1, fy)):
            for xc, wx in ((xc0, 1 - fx), (xc1, fx)):
                vel += (wz * wy * wx)[None, :] * flat(zc, yc, xc)
    return vel, inside


def _solid_at(flags_flat: np.ndarray, nx: int, ny: int, nz: int,
              pos: np.ndarray) -> np.ndarray:
    x = np.clip(np.rint(pos[:, 0]).astype(np.int64), 0, nx - 1)
    y = np.clip(np.rint(pos[:, 1]).astype(np.int64), 0, ny - 1)
    z = np.clip(np.rint(pos[:, 2]).astype(np.int64), 0, nz - 1)
    return (flags_flat[(z * ny + y) * nx + x] & BODY_MASK) == BODY_MASK


def streamlines(u: np.ndarray, flags: np.ndarray | None,
                n_lines: int = 1024,
                max_steps: int | None = None) -> dict:
    """March streamlines from the inlet plane (engine coords: flow along +y,
    seeds spread over the x-z plane at y=1).

    u: (3,nz,ny,nx) velocity; flags: flags grid ((1,nz,ny,nx) or (nz,ny,nx),
    same spatial shape) or None. Returns {"pts", "mag"} as flat float32 with
    a NaN vertex after every line, plus n_lines/total/vmax/dims counts.
    """
    u = np.ascontiguousarray(u, dtype=np.float32)
    if u.shape[0] != 3:
        raise ValueError(f"expected u with 3 components, got {u.shape}")
    _, nz, ny, nx = u.shape
    if min(nx, ny, nz) < 4:
        raise ValueError(f"grid {nx}x{ny}x{nz} too small for streamlines")
    u_flat = u.reshape(3, -1)
    fl_flat = None
    if flags is not None:
        fl = np.asarray(flags)
        fl = fl[0] if fl.ndim == 4 else fl
        fl_flat = fl.ravel().astype(np.int32)
        if fl_flat.size != nx * ny * nz:
            raise ValueError("flags grid does not match u grid")

    k = max(2, int(round(math.sqrt(max(4, n_lines)))))
    gx, gz = np.meshgrid(np.linspace(1.0, nx - 2.0, k, dtype=np.float32),
                         np.linspace(1.0, nz - 2.0, k, dtype=np.float32))
    n = k * k
    pos = np.empty((n, 3), np.float32)
    pos[:, 0], pos[:, 2] = gx.ravel(), gz.ravel()
    pos[:, 1] = 1.0

    steps = max_steps or min(4096, 2 * ny + 8)
    pts = np.full((steps + 1, n, 3), np.nan, np.float32)
    mag = np.full((steps + 1, n), np.nan, np.float32)
    pts[0] = pos
    v0, ok0 = _sample(u_flat, nx, ny, nz, pos)
    mag[0] = np.where(ok0, np.linalg.norm(v0, axis=0), np.nan)
    active = np.ones(n, bool)
    if fl_flat is not None:
        active[_solid_at(fl_flat, nx, ny, nz, pos)] = False

    for si in range(1, steps + 1):
        if not active.any():
            break
        v1, ok1 = _sample(u_flat, nx, ny, nz, pos)
        m1 = np.linalg.norm(v1, axis=0)
        live = active & ok1 & (m1 > _EPS)
        active[active & ~live] = False
        if not live.any():
            break
        li = np.flatnonzero(live)
        vhat = (v1[:, li] / m1[li]).T           # (N,3) unit directions
        mid = pos[li] + (0.5 * _STEP) * vhat
        v2, ok2 = _sample(u_flat, nx, ny, nz, mid)
        m2 = np.linalg.norm(v2, axis=0)
        npos = pos[li] + _STEP * (v2 / np.maximum(m2, _EPS)).T
        go = ok2 & (m2 > _EPS)
        if fl_flat is not None:
            go &= ~_solid_at(fl_flat, nx, ny, nz, npos)
        active[li[~go]] = False
        oki = li[go]
        pos[oki] = npos[go]
        pts[si, oki] = npos[go]
        mag[si, oki] = m2[go]

    counts = np.count_nonzero(~np.isnan(pts[:, :, 0]), axis=0)
    nverts = int(counts.sum()) + n  # +1 NaN separator vertex per line
    out_pts = np.full(nverts * 3, np.nan, np.float32)
    out_mag = np.full(nverts, np.nan, np.float32)
    w = 0   # float cursor into out_pts (3 per vertex)
    wm = 0  # vertex cursor into out_mag
    nan3 = np.array([np.nan, np.nan, np.nan], np.float32)
    for i in range(n):
        c = counts[i]
        out_pts[w:w + 3 * c] = pts[:c, i, :].ravel()
        out_pts[w + 3 * c:w + 3 * c + 3] = nan3
        out_mag[wm:wm + c] = mag[:c, i]
        out_mag[wm + c] = np.nan
        w += 3 * (c + 1)
        wm += c + 1
    vmax = float(np.nanmax(mag)) if counts.sum() else 0.0
    return {"pts": out_pts, "mag": out_mag, "n_lines": n,
            "total": nverts, "vmax": vmax,
            "dims": (nx, ny, nz)}
