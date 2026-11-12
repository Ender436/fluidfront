"""Encode path for MP4/GIF export: the imageio-ffmpeg binary must exist and
stitch a contiguous frame sequence; bad input must raise, not pass silently."""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from fluidfront.exporter import EncodeError, encode_video, ffmpeg_exe


def _write_ppms(d: str, n: int = 6, size: int = 48) -> None:
    """Trivial PPM frames (rolling gradient) - ffmpeg reads them directly,
    so the test needs no image library."""
    for i in range(n):
        g = ((np.indices((size, size)).sum(axis=0) + i * 20) % 256).astype(np.uint8)
        rgb = np.stack([g, g // 2 + i * 10, 255 - g], axis=-1)
        with open(Path(d) / f"frame_{i:05d}.ppm", "wb") as f:
            f.write(b"P6\n%d %d\n255\n" % (size, size))
            f.write(rgb.tobytes())


class TestExporter(unittest.TestCase):
    def _count_frames(self, path: Path) -> int:
        r = subprocess.run([ffmpeg_exe(), "-i", str(path), "-f", "null", "-"],
                           capture_output=True, text=True)
        m = re.search(r"frame=\s*(\d+)", r.stderr)
        return int(m.group(1)) if m else -1

    def test_binary_available(self):
        r = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0)

    def test_mp4(self):
        with tempfile.TemporaryDirectory() as d:
            _write_ppms(d)
            out = encode_video(Path(d), Path(d) / "v.mp4", fps=12, fmt="mp4",
                               ext="ppm")
            self.assertTrue(out.is_file() and out.stat().st_size > 0)
            self.assertEqual(self._count_frames(out), 6)

    def test_gif_two_pass_palette(self):
        with tempfile.TemporaryDirectory() as d:
            _write_ppms(d)
            out = encode_video(Path(d), Path(d) / "v.gif", fps=8, fmt="gif",
                               ext="ppm", gif_width=32)
            self.assertTrue(out.is_file() and out.stat().st_size > 0)
            self.assertFalse((Path(d) / "palette.png").exists(),
                             "palette temp file must be cleaned up")

    def test_unknown_fmt_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(EncodeError):
                encode_video(Path(d), Path(d) / "v.avi", fps=12, fmt="avi")

    def test_no_frames_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(EncodeError):
                encode_video(Path(d), Path(d) / "v.mp4", fps=12, fmt="mp4")


class TestExportAPI(unittest.TestCase):
    """Endpoint behaviour behind the "make video" button: begin clears the
    previous video, finish reports the file size, and web files ship with
    revalidation headers (a cached stale app.js silently kills the button)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import fluidfront.server.app as app_module
        from fluidfront.server import jobs
        cls.jobs = jobs
        cls._cases = jobs.CASES
        cls._tmp = tempfile.TemporaryDirectory()
        jobs.CASES = Path(cls._tmp.name)
        (jobs.CASES / "tiny").mkdir()
        (jobs.CASES / "tiny" / "case.json").write_text("{}")
        cls.client = TestClient(app_module.app)

    @classmethod
    def tearDownClass(cls):
        cls.jobs.CASES = cls._cases
        cls._tmp.cleanup()

    def _jpg(self, color: str) -> bytes:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.jpg"
            r = subprocess.run(
                [ffmpeg_exe(), "-y", "-f", "lavfi", "-i", f"color={color}:s=64x64",
                 "-frames:v", "1", str(p)], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr.decode()[-300:])
            return p.read_bytes()

    def test_static_files_revalidated(self):
        r = self.client.get("/index.html")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("cache-control"), "no-cache")
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.headers.get("cache-control"))

    def test_roundtrip_size_then_begin_clears(self):
        c = self.client
        out = self.jobs.CASES / "tiny" / "export.gif"
        self.assertEqual(c.post("/api/cases/tiny/export/begin").status_code, 200)
        for i, color in enumerate(("red", "green", "blue")):
            r = c.post(f"/api/cases/tiny/export/frame?i={i}", content=self._jpg(color),
                       headers={"content-type": "image/jpeg"})
            self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/cases/tiny/export/finish", json={"fps": 8, "fmt": "gif"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["frames"], 3)
        self.assertGreater(body["size"], 0)
        self.assertEqual(out.stat().st_size, body["size"])
        self.assertFalse((self.jobs.CASES / "tiny" / "export_frames").exists())
        # a new begin must wipe the previous video + frames wholesale
        self.assertEqual(c.post("/api/cases/tiny/export/begin").status_code, 200)
        self.assertFalse(out.exists())
        r = c.post("/api/cases/tiny/export/finish", json={"fps": 8, "fmt": "gif"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
