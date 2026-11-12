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


def _clear_outputs(case_dir: Path) -> None:
    """Drop the previous run's snapshots/forces so a rerun can never display
    stale frames (partially written *.vtk.* included)."""
    out = case_dir / "output"
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("*.vtk")) + list(out.glob("*.vtk.*")) \
            + [out / "forces.csv"]:
        old.unlink(missing_ok=True)


def _enqueue_event(q: asyncio.Queue, ev: dict) -> None:
    """Drop an event for a slow/stalled WebSocket instead of raising
    QueueFull inside the event loop callback (which logs a traceback)."""
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:
        pass


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


# BENCH.md calibration (MLUPs @ 256^3-class FP16S cases); first match wins.
# 3090 value is the measured steady state of a 151M-cell real case (9.7k),
# not the 256^3 benchmark peak (10.7k) - honest estimates beat optimistic.
_MLUPS_CALIB = [("3050", 2318.0), ("3090", 9700.0)]


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
        self._status: dict[str, dict] = {}   # live status cache (see read_status)

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
                self._loop.call_soon_threadsafe(_enqueue_event, q, ev)
            except RuntimeError:  # loop closing
                pass

    # ----- status.json -----------------------------------------------------
    # Live status is kept in memory (self._status); status.json is only the
    # restart-surviving copy. We must never read the file for a case we are
    # actively tracking: on Windows, a concurrent reader handle makes the
    # atomic tmp->rename fail with WinError 5 (Access is denied).
    def read_status(self, name: str) -> dict | None:
        cached = self._status.get(name)
        if cached is not None:
            return dict(cached)
        p = CASES / name / "status.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):  # partial/unreadable write
            return None

    def write_status(self, name: str, **fields) -> dict:
        status = self.read_status(name) or {}
        status.update(fields)
        status["updated"] = _now()
        self._status[name] = status
        self._persist_status(name, status)
        self._broadcast({"kind": "status", "case": name, **status})
        return status

    def _persist_status(self, name: str, status: dict) -> None:
        """Best-effort disk copy; NEVER raises (a status hiccup must not
        take down a running simulation)."""
        p = CASES / name / "status.json"
        data = json.dumps(status, indent=1)
        try:  # atomic path (fine on Linux, usually fine on Windows too)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(data)
            tmp.replace(p)
            return
        except OSError:
            pass  # Windows: rename onto a file a reader has open -> fallback
        try:  # direct write is allowed alongside shared readers; readers
              # tolerate a torn read (JSONDecodeError -> stale value)
            p.write_text(data)
        except OSError as e:
            print(f"fluidfront: warning: could not persist status for "
                  f"{name!r}: {e}", flush=True)

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
        with self._lock:
            d = case_dir(name)
            if self._active == name:
                raise BusyError("case is running - stop it first")
            if self.read_status(name):
                (d / "status.json").unlink(missing_ok=True)
            (d / "status.json.tmp").unlink(missing_ok=True)
            self._status.pop(name, None)
            shutil.rmtree(d)

    # ----- run control ------------------------------------------------------
    def start(self, name: str) -> None:
        with self._lock:
            case_dir(name)  # existence check (inside the lock: no delete race)
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

    def shutdown(self) -> None:
        """Server exiting: stop a running engine so it never outlives us as an
        orphan hogging the GPU (the worker writes status 'stopped')."""
        with self._lock:
            cancel, thread = self._cancel, self._thread
        if cancel is not None:
            cancel.set()
        if thread is not None:
            thread.join(timeout=5)

    @property
    def active(self) -> str | None:
        return self._active

    # ----- worker thread: build -> run, with live status -------------------
    def _worker(self, name: str, cancel: threading.Event) -> None:
        cdir = CASES / name
        try:
            self.write_status(name, state="building", error=None,
                              started=_now(), percent=0, t=0)
            # a rerun must never show the previous run's frames/forces: drop
            # old results before the engine starts writing new ones
            _clear_outputs(cdir)
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
                self.write_status(name, state="done", percent=100, t=t_steps)
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
    """forces.csv -> {'t': [...], 'fx': [...], ...} (evenly downsampled).

    The file is appended to while a run is active, so the last line can be
    half-written; those rows are skipped instead of letting numpy raise (which
    used to turn a perfectly normal live poll into a 500).
    """
    import numpy as np
    p = case_dir(name) / "output" / "forces.csv"
    if not p.is_file():
        return None
    rows: list[list[float]] = []
    try:
        with open(p, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) < 6:  # partial final line (run in progress)
                    continue
                try:
                    rows.append([float(v) for v in parts[:6]])
                except ValueError:  # torn write / stray text
                    continue
    except OSError:
        return None
    if not rows:
        return None
    data = np.asarray(rows, dtype=float)
    if len(data) > max_rows:
        # keep the whole trend (both ends) while bounding the payload
        idx = np.linspace(0, len(data) - 1, max_rows).round().astype(int)
        data = data[idx]
    cols = ["t", "fx", "fy", "fz", "cd", "cl"]
    return {c: data[:, i].tolist() for i, c in enumerate(cols)}


def snapshots(name: str) -> dict[str, list[int]]:
    """Available VTK snapshots: {'u': [t...], 'rho': [...], 'flags': [...]}."""
    out_dir = case_dir(name) / "output"
    res: dict[str, list[int]] = {}
    for f in out_dir.glob("*.vtk"):
        base, _, tnum = f.stem.rpartition("-")
        if base and tnum.isdigit():
            res.setdefault(base, []).append(int(tnum))
    return {k: sorted(v) for k, v in res.items()}


def _vtk_candidates(out_dir: Path, which: str, t: int | None,
                    name: str = "") -> list[Path]:
    """All `which-*.vtk` snapshots at step <= t, oldest first."""
    files = sorted(out_dir.glob(f"{which}-*.vtk"))
    if not files:
        raise NotFound(f"no '{which}' snapshots for case {name!r}")
    if t is not None:
        files = [f for f in files
                 if int(f.stem.rpartition("-")[2]) <= t] or files[:1]
    return files


def _pick_vtk(out_dir: Path, which: str, t: int | None, name: str = "") -> Path:
    """Latest `which-*.vtk` snapshot at step <= t."""
    return _vtk_candidates(out_dir, which, t, name)[-1]


def _load_latest_vtk(out_dir: Path, which: str, t: int | None,
                     name: str = ""):
    """Newest readable snapshot. The engine writes VTK files directly, so the
    newest one can be half-written while a run is active; fall back to the
    previous complete frame instead of failing the request."""
    from ..vtkio import load_vtk
    last: Exception | None = None
    for f in reversed(_vtk_candidates(out_dir, which, t, name)):
        try:
            return load_vtk(f)
        except (ValueError, IndexError, KeyError, OSError) as e:
            last = e
    raise ValueError(f"no readable '{which}' snapshot for case {name!r}: {last}")


def read_q(name: str, t: int | None = None, grid: int = 96):
    """Q-criterion vortex grid for the viewer (see fluidfront.qcriterion).

    Like read_field, skip a half-written newest snapshot while a run is
    active and use the previous complete frame instead."""
    from ..qcriterion import q_criterion
    out_dir = case_dir(name) / "output"
    last: Exception | None = None
    for u in reversed(_vtk_candidates(out_dir, "u", t, name)):
        t_actual = int(u.stem.rpartition("-")[2])
        try:
            fl = _pick_vtk(out_dir, "flags", t, name)
        except NotFound:
            fl = None
        try:
            return q_criterion(u, fl, grid=grid), t_actual
        except (ValueError, IndexError, KeyError, OSError) as e:
            last = e
    raise ValueError(f"no readable 'u' snapshot for case {name!r}: {last}")


def read_field(name: str, which: str = "u", t: int | None = None,
               max_points: int = 200_000):
    """Latest snapshot of `which` at step <= t, downsampled.

    Returns (field, dims): field is float32 (ncomp, nsz, ny, nx) little-endian,
    dims = [nx, ny, nz, ncomp, t_actual].
    """
    import numpy as np
    out_dir = case_dir(name) / "output"
    snap = _load_latest_vtk(out_dir, which, t, name)
    field = snap["field"].astype(np.float32)
    ncomp, nz, ny, nx = field.shape
    n = nx * ny * nz
    if n > max_points:
        stride = max(2, round((n / max_points) ** (1 / 3)))
        field = field[:, ::stride, ::stride, ::stride]
        nz, ny, nx = field.shape[1:]
    t_actual = int(snap["t"])
    return np.ascontiguousarray(field), [nx, ny, nz, ncomp, t_actual]
