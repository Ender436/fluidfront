"""Run a compiled FluidX3D case, streaming progress to the app.

The engine's progress table lines look like:
    |    2318 |    178 GB/s |       138 |         5660  57% |                 31s |
they give MLUPs, bandwidth, ms/step-chunk, current step, %, and an ETA.
We parse them into events for the server (WebSocket progress) and mirror
the whole stdout to <case>/run.log.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

import ctypes


def _pdeath_sigterm():
    """Linux prctl(PR_SET_PDEATHSIG, SIGTERM): the kernel terminates the
    engine even if we die ungracefully (double Ctrl+C, SIGKILL, crash)."""
    try:
        ctypes.CDLL("libc.so.6").prctl(1, 15, 0, 0, 0)
    except Exception:  # non-critical nicety; graceful shutdown covers us
        pass
from typing import Callable

_PROGRESS = re.compile(
    r"\|\s*(\d+)\s*\|\s*([\d.]+)\s*GB/s\s*\|\s*(\d+)\s*\|\s*(\d+)\s+(\d+)%\s*\|\s*([^|]+?)\s*\|"
)

Event = dict  # {"kind": "progress"|"info", ...}
Callback = Callable[[Event], None]


def parse_progress(line: str) -> dict | None:
    m = _PROGRESS.search(line)
    if not m:
        return None
    mlups, gbs, ms, step, pct, eta = m.groups()
    return {"kind": "progress", "mlups": int(mlups), "gbs": float(gbs),
            "step": int(step), "percent": int(pct), "eta": eta.strip()}


def run_case(binary: Path, case_dir: Path, on_event: Callback | None = None,
             cancel: threading.Event | None = None) -> int:
    """Blocking run of the engine for `case_dir`. Returns exit code.
    `cancel`: if a threading.Event is given and set, the engine process is
    terminated (case stop button)."""
    binary, case_dir = Path(binary), Path(case_dir)
    log_path = case_dir / "run.log"
    rc = 0
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [str(binary)], cwd=str(case_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            preexec_fn=_pdeath_sigterm,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                break
            line = line.rstrip("\r\n")
            log.write(line + "\n")
            log.flush()
            if on_event:
                ev = parse_progress(line)
                if ev is None:
                    ev = {"kind": "info", "line": line}
                on_event(ev)
        rc = proc.wait()
    return rc
