"""Q-criterion vortex extraction from a velocity snapshot (M6 visualization).

Q = 0.5*(|Omega|^2 - |S|^2) measures local rotation rate minus shear rate;
positive Q marks vortex cores. We compute it from the engine's physical-unit
velocity VTK snapshot with central differences, mask the solid body, and
resample to a uniform cube the browser can march-cubes directly.

Accuracy note: this is a visualization filter on a coarse downsampled grid,
NOT a measurement - force numbers never touch this code path.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .vtkio import load_vtk

GRID = 96  # output cube resolution (browser marching-cubes input)


def _q_from_field(field: np.ndarray, sp: list) -> np.ndarray:
    """Q = -1/2 tr(G*G) with G = grad(u), for a (3,nz,ny,nx) field and
    spacings sp = [dx, dy, dz]. Expanded:
    Q = -( sum_{i<j} g_ij*g_ji + 1/2*sum_i g_ii^2 )
    e.g. solid rotation (+/-w): Q = +w^2; pure shear: Q = 0.
    np.gradient returns [d/dz, d/dy, d/dx], so the off-diagonal pairs are
    g_xy*g_yx = du[1]*dv[2], g_xz*g_zx = du[0]*dw[2],
    g_yz*g_zy = dv[0]*dw[1]. (Mixing in the diagonal terms here silently
    drops vortices whose axis is not z - e.g. the streamwise wake vortices
    that dominate a wind-tunnel view.)
    """
    u, v, w = field  # velocity x,y,z components (SI m/s)
    du = np.gradient(u, sp[2], sp[1], sp[0])
    dv = np.gradient(v, sp[2], sp[1], sp[0])
    dw = np.gradient(w, sp[2], sp[1], sp[0])
    return -(du[1] * dv[2] + du[0] * dw[2] + dv[0] * dw[1]
             + 0.5 * (du[2] * du[2] + dv[1] * dv[1] + dw[0] * dw[0]))


def q_criterion(u_vtk: Path, flags_vtk: Path | None = None,
                grid: int = GRID) -> dict:
    """Compute a grid^3 Q-field from a u-*.vtk snapshot.

    Returns {"q": float32 (grid,grid,grid), "iso_lo": .., "iso_hi": ..}:
    iso_lo/iso_hi bracket sensible iso-levels (1/s^2) for the UI slider.
    """
    snap = load_vtk(u_vtk)
    field = np.ascontiguousarray(snap["field"], dtype=np.float32)  # (3,nz,ny,nx)
    nx, ny, nz = snap["dims"]
    sp = snap["spacing"]  # physical dx, dy, dz (m); VTK export is SI
    if len(sp) != 3 or sp[0] <= 0:
        raise ValueError(f"{u_vtk}: bad VTK spacing {sp}")

    # decimate first: iso-surfaces can't show detail finer than `grid` anyway
    k = max(1, math.ceil(max(nx, ny, nz) / (grid * 1.5)))
    if k > 1:
        field = np.ascontiguousarray(field[:, ::k, ::k, ::k])
        sp = [s * k for s in sp]
    nzc, nyc, nxc = field.shape[1:]

    q = _q_from_field(field, sp)
    del field

    if flags_vtk is not None:
        fl = np.ascontiguousarray(load_vtk(flags_vtk)["field"])
        if fl.ndim == 4:  # loader shape (ncomp,nz,ny,nx); flags has ncomp=1
            fl = fl[0]
        if k > 1:
            fl = fl[::k, ::k, ::k]
        if fl.shape == q.shape:
            body = ((fl.astype(np.int16) & 65) == 65)  # TYPE_S | TYPE_X
            q[body] = 0.0
        del fl

    qr = _resize3(q, grid)
    pos = qr[qr > 0]
    if pos.size >= 100:
        # iso-slider range [iso_lo, iso_hi]: noise near walls gives billions of
        # epsilon-positive cells, so judge everything relative to the max
        iso_hi = float(pos.max())
        strong = pos[pos > 1e-3 * iso_hi]  # cells worth surfacing at all
        iso_lo = float(np.exp(np.log(strong).mean())) if strong.size else iso_hi / 100
        iso_lo = min(max(iso_lo, iso_hi * 1e-3), iso_hi * 0.9)
    else:
        iso_lo = iso_hi = 0.0
    return {"q": qr, "iso_lo": iso_lo, "iso_hi": iso_hi,
            "dims": (grid, grid, grid), "full_dims": (nx, ny, nz)}


def _resize3(q: np.ndarray, n: int) -> np.ndarray:
    """Trilinear-resample (nz,ny,nx) -> (n,n,n) with numpy gather (no scipy)."""
    nz, ny, nx = q.shape

    def ax(m: int):
        i = np.linspace(0, m - 1, n)
        i0 = np.floor(i).astype(np.int64)
        return i0, np.minimum(i0 + 1, m - 1), i - i0

    z0, z1, wz = ax(nz)
    y0, y1, wy = ax(ny)
    x0, x1, wx = ax(nx)
    wz = wz[:, None, None]
    wy = wy[None, :, None]
    wx = wx[None, None, :]

    c000 = q[z0[:, None, None], y0[None, :, None], x0[None, None, :]]
    c001 = q[z0[:, None, None], y0[None, :, None], x1[None, None, :]]
    c010 = q[z0[:, None, None], y1[None, :, None], x0[None, None, :]]
    c011 = q[z0[:, None, None], y1[None, :, None], x1[None, None, :]]
    c100 = q[z1[:, None, None], y0[None, :, None], x0[None, None, :]]
    c101 = q[z1[:, None, None], y0[None, :, None], x1[None, None, :]]
    c110 = q[z1[:, None, None], y1[None, :, None], x0[None, None, :]]
    c111 = q[z1[:, None, None], y1[None, :, None], x1[None, None, :]]
    c00 = c000 * (1 - wx) + c001 * wx
    c01 = c010 * (1 - wx) + c011 * wx
    c10 = c100 * (1 - wx) + c101 * wx
    c11 = c110 * (1 - wx) + c111 * wx
    c0 = c00 * (1 - wy) + c01 * wy
    c1 = c10 * (1 - wy) + c11 * wy
    return (c0 * (1 - wz) + c1 * wz).astype(np.float32)
