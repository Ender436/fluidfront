"""Case + job management for the app server.

Cases live in `cases/<name>/` (case.json + model.stl + output/ + status.json).
Exactly ONE case runs at a time: the engine uses a single shared build tree
(engine/FluidX3D/src), so cases compile and run serially. Status is persisted
to `<case>/status.json`, so the UI survives server restarts; progress events
are additionally broadcast to WebSocket subscribers.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..gen import build as gbuild
from ..gen.runner import run_case
from ..gen.schema import load_case
from ..gen.units import CaseError
from ..geometry.loaders import GeometryError

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "cases"

STATES = ("created", "building", "running", "done", "failed", "stopped")


class NotFound(Exception):
    pass


class BusyError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def device_info() -> dict:
    """GPU name + memory budget for quality-tier capping (nvidia-smi)."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            name, total, used = r.stdout.strip().splitlines()[0].split(",")
            total, used = float(total), float(used)
            return {"name": name.strip(), "total_mi": total, "used_mi": used,
                    "free_mi": total - used, "source": "nvidia-smi",
                    # FluidX3D allocates the lattice in ONE OpenCL buffer, and
                    # drivers cap single allocations (measured: 955 MB of 3822
                    # usable on RTX 3050 Ti laptop) -> conservative total/4
                    "maxbuf_mi": total * 0.25}
    except Exception:  # noqa: BLE001 - any nvidia-smi hiccup -> conservative default
        pass
    return {"name": "unknown GPU (assuming 4 GB)", "total_mi": 4096.0,
            "used_mi": 0.0, "free_mi": 4096.0, "source": "fallback",
            "maxbuf_mi": 1024.0}


# BENCH.md calibration (MLUPs @ 256^3-class FP16S cases); first match wins
_MLUPS_CALIB = [("3050", 2318.0), ("3090", 10732.0)]


def estimated_seconds(cells: int, t_steps: int, device: dict | None = None,
                      subgrid: bool = False) -> float:
    """Pre-run 'about how long?' promise; live progress refines it later."""
    dev = device or device_info()
    name = dev["name"].lower()
    mlups = next((m for k, m in _MLUPS_CALIB if k in name), 2000.0)
    if subgrid:  # measured on 3050 Ti: Smagorinsky LES ~ 1.6x slower than SRT
        mlups /= 1.6
    return cells * t_steps / (mlups * 1e6)


def case_dir(name: str) -> Path:
    d = CASES / name
    if not (d / "case.json").is_file():
        raise NotFound(f"no such case: {name!r}")
    return d


class JobManager:
    """Serial run queue with live status + event broadcast."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: str | None = None      # case being built/run
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._subs: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # ----- WebSocket fan-out (events originate from the worker thread) ----
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def _broadcast(self, ev: dict) -> None:
        if self._loop is None:
            return
        for q in list(self._subs):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:  # queue full / loop closing
                pass

    # ----- status.json -----------------------------------------------------
    def read_status(self, name: str) -> dict | None:
        p = CASES / name / "status.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None

    def write_status(self, name: str, **fields) -> dict:
        p = CASES / name / "status.json"
        status = self.read_status(name) or {}
        status.update(fields)
        status["updated"] = _now()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, indent=1))
        tmp.replace(p)
        self._broadcast({"kind": "status", "case": name, **status})
        return status

    # ----- listing / deletion ----------------------------------------------
    def list_cases(self) -> list[dict]:
        if not CASES.is_dir():
            return []
        out = []
        for d in sorted(CASES.iterdir()):
            if d.is_dir() and (d / "case.json").is_file():
                st = self.read_status(d.name) or {}
                out.append({"name": d.name,
                            "state": st.get("state", "created"),
                            "percent": st.get("percent", 0),
                            "error": st.get("error"),
                            "updated": st.get("updated")})
        return out

    def delete(self, name: str) -> None:
        import shutil
        d = case_dir(name)
        with self._lock:
            if self._active == name:
                raise BusyError("case is running - stop it first")
        if self.read_status(name):
            (d / "status.json").unlink(missing_ok=True)
        shutil.rmtree(d)

    # ----- run control ------------------------------------------------------
    def start(self, name: str) -> None:
        case_dir(name)  # existence check
        with self._lock:
            if self._active is not None:
                raise BusyError(
                    f"case {self._active!r} is using the solver; wait for it "
                    "to finish (one run at a time)")
            self._active = name
            self._cancel = threading.Event()
            self._thread = threading.Thread(target=self._worker,
                                            args=(name, self._cancel),
                                            daemon=True)
            self._thread.start()

    def stop(self, name: str) -> None:
        with self._lock:
            if self._active != name or self._cancel is None:
                raise NotFound(f"case {name!r} is not running")
            self._cancel.set()

    @property
    def active(self) -> str | None:
        return self._active

    # ----- worker thread: build -> run, with live status -------------------
    def _worker(self, name: str, cancel: threading.Event) -> None:
        cdir = CASES / name
        try:
            self.write_status(name, state="building", error=None,
                              started=_now(), percent=0, t=0)
            spec = load_case(cdir)
            gbuild.write_case_files(spec)
            binary = gbuild.build()
            if cancel.is_set():
                self.write_status(name, state="stopped")
                return
            t_steps = int(spec.run.get("t_steps", 0))
            self.write_status(name, state="running", t=0, t_steps=t_steps)

            def on_event(ev: dict) -> None:
                if ev["kind"] == "progress":
                    self.write_status(name, state="running", t=ev["step"],
                                      t_steps=t_steps, percent=ev["percent"],
                                      mlups=ev["mlups"], gbs=ev["gbs"],
                                      eta=ev["eta"])

            rc = run_case(binary, cdir, on_event=on_event, cancel=cancel)
            if cancel.is_set():
                self.write_status(name, state="stopped")
            elif rc == 0:
                self.write_status(name, state="done", percent=100)
            else:
                self.write_status(name, state="failed",
                                  error=f"engine exited with code {rc} "
                                        "(see run.log)")
        except (CaseError, GeometryError, gbuild.BuildError) as e:
            self.write_status(name, state="failed", error=str(e))
        except Exception as e:  # noqa: BLE001 - never kill the manager
            self.write_status(name, state="failed", error=f"internal error: {e}")
        finally:
            with self._lock:
                self._active = None
                self._cancel = None
                self._thread = None


# ---- results readers (forces + downsampled fields) -------------------------

def read_forces(name: str, max_rows: int = 1500) -> dict | None:
    """forces.csv -> {'t': [...], 'fx': [...], ...} (downsampled tail-kept)."""
    import numpy as np
    p = case_dir(name) / "output" / "forces.csv"
    if not p.is_file():
        return None
    data = np.loadtxt(p, delimiter=",", comments="#")
    if data.size == 0:
        return None
    data = np.atleast_2d(data)
    if len(data) > max_rows:
        step = -(-len(data) // max_rows)
        data = data[::step]
    cols = ["t", "fx", "fy", "fz", "cd", "cl"]
    return {c: data[:, i].tolist() for i, c in enumerate(cols) if i < data.shape[1]}


def snapshots(name: str) -> dict[str, list[int]]:
    """Available VTK snapshots: {'u': [t...], 'rho': [...], 'flags': [...]}."""
    out_dir = case_dir(name) / "output"
    res: dict[str, list[int]] = {}
    for f in out_dir.glob("*.vtk"):
        base, _, tnum = f.stem.rpartition("-")
        if base and tnum.isdigit():
            res.setdefault(base, []).append(int(tnum))
    return {k: sorted(v) for k, v in res.items()}


def read_field(name: str, which: str = "u", t: int | None = None,
               max_points: int = 200_000):
    """Latest snapshot of `which` at step <= t, downsampled.

    Returns (field, dims): field is float32 (ncomp, nsz, ny, nx) little-endian,
    dims = [nx, ny, nz, ncomp, t_actual].
    """
    import numpy as np
    from ..vtkio import load_vtk
    out_dir = case_dir(name) / "output"
    files = sorted(out_dir.glob(f"{which}-*.vtk"))
    if not files:
        raise NotFound(f"no '{which}' snapshots for case {name!r}")
    if t is not None:
        files = [f for f in files
                 if int(f.stem.rpartition("-")[2]) <= t] or files[:1]
    snap = load_vtk(files[-1])
    field = snap["field"].astype(np.float32)
    ncomp, nz, ny, nx = field.shape
    n = nx * ny * nz
    if n > max_points:
        stride = max(2, round((n / max_points) ** (1 / 3)))
        field = field[:, ::stride, ::stride, ::stride]
        nz, ny, nx = field.shape[1:]
    t_actual = int(files[-1].stem.rpartition("-")[2])
    return np.ascontiguousarray(field), [nx, ny, nz, ncomp, t_actual]
