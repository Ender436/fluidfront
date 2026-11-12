"""Analytic checks for the Q-criterion kernel (regression lock).

An earlier version of the off-diagonal expansion only produced positive Q
for vortex axes along z, which an analytic test limited to z-rotations
missed. Every axis is covered here for exactly that reason.
"""

import unittest

import numpy as np

from fluidfront.qcriterion import _q_from_field

N, DX, OMEGA = 16, 0.1, 2.0


def _grid():
    c = (np.arange(N) - (N - 1) / 2) * DX
    return np.meshgrid(c, c, c, indexing="ij")  # Z, Y, X each (nz, ny, nx)


def _rotating(axis):
    """Solid-body rotation u = omega x r (omega along the given axis)."""
    Z, Y, X = _grid()
    z = np.zeros_like(X)
    if axis == "x":
        u = (z, -OMEGA * Z, OMEGA * Y)
    elif axis == "y":
        u = (OMEGA * Z, z, -OMEGA * X)
    else:
        u = (-OMEGA * Y, OMEGA * X, z)
    return np.stack([a.astype(np.float64) for a in u])


class TestQFromField(unittest.TestCase):
    def test_rotation_positive_on_every_axis(self):
        # solid rotation: Q = +|omega|^2 everywhere (exact for linear fields)
        for axis in ("x", "y", "z"):
            q = _q_from_field(_rotating(axis), [DX, DX, DX])
            self.assertTrue(np.all(np.isfinite(q)), f"{axis}: non-finite")
            self.assertTrue(np.all(q > 0.5 * OMEGA**2),
                            f"rotation about {axis}: Q must be positive "
                            f"(got min {q.min():.4f}, want {OMEGA**2:.4f})")
            np.testing.assert_allclose(q, OMEGA**2, rtol=1e-3,
                                       err_msg=f"rotation about {axis}")

    def test_pure_shear_is_zero(self):
        Z, Y, X = _grid()
        field = np.stack([1.5 * Y, np.zeros_like(X), np.zeros_like(X)])
        q = _q_from_field(field, [DX, DX, DX])
        np.testing.assert_allclose(q, 0.0, atol=1e-9)

    def test_pure_strain_is_negative(self):
        Z, Y, X = _grid()
        field = np.stack([0.7 * X, -0.7 * Y, np.zeros_like(X)])
        q = _q_from_field(field, [DX, DX, DX])
        np.testing.assert_allclose(q, -0.49, rtol=1e-3)

    def test_uniform_flow_is_zero(self):
        Z, Y, X = _grid()
        field = np.stack([np.full_like(X, 3.0)] * 3)
        q = _q_from_field(field, [DX, DX, DX])
        np.testing.assert_allclose(q, 0.0, atol=1e-9)


if __name__ == "__main__":
    unittest.main()
