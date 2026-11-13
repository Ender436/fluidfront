"""Streamline tracing: analytic fields must give exact geometry, and the
/api streamlines endpoint must guard 404/503 like the other data routes."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from fluidfront.viz import streamlines


def _write_vtk(path: Path, field: np.ndarray, name: str = "u") -> None:
    """Minimal FluidX3D-v3.8-style binary VTK (big-endian AoS), i.e. the
    exact inverse of fluidfront.vtkio.load_vtk."""
    if field.ndim == 3:
        field = field[None]
    ncomp, nz, ny, nx = field.shape
    tag, be = ("FLOAT 3", ">f4") if field.dtype == np.float32 \
        else ("UNSIGNED_CHAR 1", ">u1")
    hdr = (f"# vtk DataFile Version 3.0\nfluidfront test\nBINARY\n"
           f"DATASET STRUCTURED_POINTS\nDIMENSIONS {nx} {ny} {nz}\n"
           f"ORIGIN 0 0 0\nSPACING 0.01 0.01 0.01\n"
           f"POINT_DATA {nx * ny * nz}\nSCALARS {name} {tag}\n"
           "LOOKUP_TABLE default\n")
    aos = np.ascontiguousarray(np.moveaxis(field, 0, -1)).astype(be)
    path.write_bytes(hdr.encode("ascii") + aos.tobytes())


def _split(res: dict) -> list[np.ndarray]:
    """pts flat array -> list of (len,3) line arrays (NaN separators)."""
    pts = res["pts"].reshape(-1, 3)
    lines, cur = [], []
    for v in pts:
        if np.isnan(v[0]):
            if cur:
                lines.append(np.array(cur, np.float32))
            cur = []
        else:
            cur.append(v)
    return lines


def _uniform(nx=32, ny=64, nz=32, U=2.0) -> np.ndarray:
    u = np.zeros((3, nz, ny, nx), np.float32)
    u[1] = U  # engine flow axis is +y
    return u


class TestStreamlines(unittest.TestCase):
    def test_uniform_flow_straight_full_length(self):
        u, fl = _uniform(), np.zeros((1, 32, 64, 32), np.uint8)
        res = streamlines(u, fl, n_lines=64)
        self.assertEqual(res["n_lines"], 64)
        self.assertAlmostEqual(res["vmax"], 2.0, places=5)
        lines = _split(res)
        self.assertEqual(len(lines), 64)
        for ln in lines:
            self.assertGreater(len(ln), 70)          # crossed most of ny=64
            self.assertLess(ln[-1][1], 64)           # stopped at y boundary
            self.assertGreater(ln[-1][1], 60)
            span_x = ln[:, 0].max() - ln[:, 0].min()  # straight along +y
            span_z = ln[:, 2].max() - ln[:, 2].min()
            self.assertLess(span_x, 1e-3)
            self.assertLess(span_z, 1e-3)
            self.assertTrue(np.all(np.diff(ln[:, 1]) > 0))

    def test_solid_slab_stops_and_deflects(self):
        u, fl = _uniform(), np.zeros((1, 32, 64, 32), np.uint8)
        fl[0, 12:21, 20:31, 12:21] = 65  # TYPE_S|TYPE_X slab across part of the plane
        res = streamlines(u, fl, n_lines=64)
        lines = _split(res)
        stopped = [ln for ln in lines if ln[-1][1] < 21]
        passed = [ln for ln in lines if ln[-1][1] > 60]
        self.assertGreater(len(stopped), 0, "slab footprint must block seeds")
        self.assertGreater(len(passed), 0, "seeds off the slab must cross")
        self.assertEqual(len(stopped) + len(passed), len(lines))
        for ln in stopped:  # last point just before the slab face at y=20
            self.assertGreater(ln[-1][1], 18)
            self.assertLess(ln[-1][1], 20.5)

    def test_stagnation_field_yields_single_points(self):
        u, fl = _uniform(U=0.0), np.zeros((1, 32, 64, 32), np.uint8)
        res = streamlines(u, fl, n_lines=16)
        self.assertEqual(res["vmax"], 0.0)
        for ln in _split(res):
            self.assertEqual(len(ln), 1)

    def test_flags_shape_mismatch_raises(self):
        with self.assertRaises(ValueError):
            streamlines(_uniform(), np.zeros((1, 8, 8, 8), np.uint8))

    def test_no_flags_integrates_freely(self):
        res = streamlines(_uniform(), None, n_lines=16)
        self.assertEqual(len(_split(res)), 16)
        self.assertTrue(all(len(ln) > 70 for ln in _split(res)))


class TestStreamlinesAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import fluidfront.server.app as app_module
        from fluidfront.server import jobs
        cls.jobs = jobs
        cls._cases = jobs.CASES
        cls._tmp = tempfile.TemporaryDirectory()
        jobs.CASES = Path(cls._tmp.name)
        for n in ("slip", "nosnap"):
            (jobs.CASES / n).mkdir()
            (jobs.CASES / n / "case.json").write_text("{}")
        out = jobs.CASES / "slip" / "output"
        out.mkdir()
        _write_vtk(out / "u-000000100.vtk", _uniform(16, 32, 16), name="u")
        _write_vtk(out / "flags-000000000.vtk",
                   np.zeros((1, 16, 32, 16), np.uint8), name="flags")
        cls.client = TestClient(app_module.app)

    @classmethod
    def tearDownClass(cls):
        cls.jobs.CASES = cls._cases
        cls._tmp.cleanup()

    def test_roundtrip_meta_and_buffer(self):
        import json
        r = self.client.get("/api/cases/slip/streamlines?lines=16")
        self.assertEqual(r.status_code, 200, r.text)
        meta = json.loads(r.headers["X-Meta"])
        self.assertEqual(meta["t"], 100)
        self.assertEqual(meta["dims"], [16, 32, 16])
        self.assertAlmostEqual(meta["vmax"], 2.0, places=4)
        buf = np.frombuffer(r.content, dtype="<f4")
        self.assertEqual(buf.size, meta["total"] * 4)  # 3f pos + 1f mag

    def test_unknown_case_404(self):
        self.assertEqual(
            self.client.get("/api/cases/nope/streamlines").status_code, 404)

    def test_case_without_snapshots_404(self):
        r = self.client.get("/api/cases/nosnap/streamlines")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
