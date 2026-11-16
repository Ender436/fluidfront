"""Duration control (M6f): tunnel-throughs -> run length, timeline/video
frame count, and the disk cap that keeps huge grids' snapshots sane."""

import tempfile
import unittest
from pathlib import Path

import trimesh

from fluidfront.geometry.pipeline import build_tunnel_case


def _box_stl(dirpath: Path) -> Path:
    m = trimesh.creation.box(extents=(2.0, 1.0, 1.0))  # watertight, 2 m long
    p = dirpath / "box.stl"
    m.export(p)
    return p


class TestDuration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.stl = _box_stl(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, throughs, **kw):
        return build_tunnel_case(self.stl, self.dir / "c", u_si=10.0,
                                 tier="preview", throughs=throughs, **kw)

    def test_duration_scales_run_length(self):
        std = self.build(4.0)[0]["run"]["t_steps"]
        quick, long_ = self.build(2.0)[0]["run"]["t_steps"], \
            self.build(8.0)[0]["run"]["t_steps"]
        self.assertLess(quick, std)
        self.assertGreater(long_, 1.9 * std)  # ~2x per doubling of throughs

    def test_frames_grow_with_duration(self):
        f_std = self.build(4.0)[1]["frames"]
        f_quick, f_long = self.build(2.0)[1]["frames"], self.build(8.0)[1]["frames"]
        self.assertGreaterEqual(f_quick, 8)  # never back down to 8-frame videos
        self.assertGreater(f_std, f_quick)
        self.assertGreater(f_long, f_std)
        self.assertLessEqual(f_long, 32)

    def test_frames_match_case_json(self):
        case, rep = self.build(4.0)
        run = case["run"]
        self.assertEqual(
            rep["frames"],
            run["t_steps"] // run["snapshot_every"] +
            bool(run["t_steps"] % run["snapshot_every"]))

    def test_huge_grid_caps_frames_and_warns(self):
        # pure layout math (no voxelization), so 512 LU is cheap to test
        case, rep = self.build(8.0, size_lu=512)
        self.assertLess(rep["frames"], 32)
        self.assertEqual(rep["frames"], 8)  # 8 frames is the absolute floor
        self.assertTrue(any("frames capped" in w for w in rep["warnings"]))
        # the cap must actually bite at realistic sizes: half the target
        half = self.build(8.0, size_lu=256)
        self.assertLess(half[1]["frames"], 32)


if __name__ == "__main__":
    unittest.main()
