"""Run a compiled FluidX3D case, streaming progress to the app.

The engine's progress table lines look like:
    |    2318 |    178 GB/s |       138 |         5660  57% |                 31s |
they give MLUPs, bandwidth, ms/step-chunk, current step, %, and an ETA.
We parse them into events for the server (WebSocket progress) and mirror
the whole stdout to <case>/run.log.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable


def _pdeath_sigterm():
    """Linux prctl(PR_SET_PDEATHSIG, SIGTERM): the kernel terminates the
    engine even if we die ungracefully (double Ctrl+C, SIGKILL, crash)."""
    try:
        ctypes.CDLL("libc.so.6").prctl(1, 15, 0, 0, 0)
    except Exception:  # non-critical nicety; graceful shutdown covers us
        pass


# Windows job-object structures, per the Win32 SDK (JobObjectExtendedLimit-
# Information, class 9). Field order matters: LimitFlags sits at offset 16
# (after two LARGE_INTEGER time limits), not first. Fixed-width types keep
# the layout byte-identical on every host (c_ulong is 8 bytes on Linux but
# 4 on Windows x64); x64 Windows is the only target, where c_size_t == 8.
class _BasicLim(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_uint64),
                ("MaximumWorkingSetSize", ctypes.c_uint64),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_uint64),  # ULONG_PTR
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadBytes", "WriteBytes", "OtherBytes",
        "ReadOperations", "WriteOperations", "OtherOperations")]


class _ExtLim(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLim),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_uint64),
                ("JobMemoryLimit", ctypes.c_uint64),
                ("PeakProcessMemoryUsed", ctypes.c_uint64),
                ("PeakJobMemoryUsed", ctypes.c_uint64)]


def _win_assign_kill_job(proc) -> int | None:
    """Windows counterpart of PR_SET_PDEATHSIG: put the engine in a Job Object
    with KILL_ON_CLOSE; closing our handle (i.e. any process death) kills it.
    Returns the job handle to keep open until the engine exits."""
    try:
        k32 = ctypes.windll.kernel32
        # explicit signatures: without restype, handles come back as 32-bit
        # ints and get truncated on 64-bit Windows
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.restype = ctypes.c_int
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                ctypes.c_void_p, ctypes.c_ulong]
        k32.AssignProcessToJobObject.restype = ctypes.c_int
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtLim()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_CLOSE
        ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                         ctypes.sizeof(info))
        if not (ok and k32.AssignProcessToJobObject(job, int(proc._handle))):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:  # best-effort nicety; graceful shutdown covers us
        return None

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
            text=True, bufsize=1, errors="replace",
            preexec_fn=_pdeath_sigterm if os.name == "posix" else None,
        )
        job = _win_assign_kill_job(proc) if os.name == "nt" else None
        if cancel is not None:  # watchdog: stop must work even while the
            def _watch() -> None:  # engine is silent (kernel compile, hang)
                while proc.poll() is None:
                    if cancel.wait(0.5):
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()  # bounded escalate, then release stdout
                        return
            threading.Thread(target=_watch, daemon=True).start()
        assert proc.stdout is not None
        try:
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
                    try:
                        on_event(ev)
                    except Exception as e:  # noqa: BLE001 - UI callback must
                        print(f"fluidfront: progress handler error: {e}",
                              flush=True)  # never stop the solver
            rc = proc.wait()
        finally:
            # any exception in the loop above (logging, progress, ...) must
            # never orphan a solver that keeps burning the GPU
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.stdout.close()  # release the pipe fd without waiting for GC
            if job is not None:  # closing the job handle releases the kill-guard
                ctypes.windll.kernel32.CloseHandle(job)
    return rc
