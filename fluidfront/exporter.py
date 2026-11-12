"""Encode captured viewer frames into MP4/GIF.

Uses the ffmpeg binary bundled with imageio-ffmpeg (a pip wheel): no system
ffmpeg install, and it works identically on Windows - important since the
frame capture happens in the browser and only encoding is local.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class EncodeError(Exception):
    pass


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001 - pip install is the fix, tell them
        raise EncodeError(f"video encoder unavailable (pip install "
                          f"imageio-ffmpeg): {e}") from e


def encode_video(frames_dir: Path, out_path: Path, fps: int, fmt: str,
                 ext: str = "jpg", gif_width: int = 640) -> Path:
    """frames_dir holds frame_00000.<ext>, frame_00001.<ext>, ... (contiguous,
    starting at 0). fmt: "mp4" | "gif"."""
    ff = ffmpeg_exe()
    src = str(frames_dir / f"frame_%05d.{ext}")
    pal = frames_dir / "palette.png"
    if fmt == "mp4":
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-framerate", str(fps), "-i", src,
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # h264: even dims
               "-c:v", "libx264", "-preset", "medium", "-crf", "23",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)]
    elif fmt == "gif":
        vf = f"fps={fps},scale={gif_width}:-2:flags=lanczos"
        r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", src, "-vf", vf + ",palettegen", str(pal)],
                           capture_output=True, text=True)
        if r.returncode:
            raise EncodeError(f"gif palette pass failed: {r.stderr.strip()[-300:]}")
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-i", src, "-i", str(pal),
               "-lavfi", f"{vf}[x];[x][1:v]paletteuse", str(out_path)]
    else:
        raise EncodeError(f"unknown video format {fmt!r} (mp4/gif)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not out_path.is_file():
        raise EncodeError(f"encoding failed: {r.stderr.strip()[-300:]}")
    pal.unlink(missing_ok=True)
    return out_path
