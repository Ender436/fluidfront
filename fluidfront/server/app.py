"""Fluidfront app server: case CRUD, run queue, live results.

The WebUI talks to exactly this surface:
- REST: cases (create from model upload / list / detail / delete), run/stop,
  forces, snapshot list, downsampled field data, device budget.
- WebSocket /ws: status/progress events for every case (persisted mirror:
  <case>/status.json).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (FastAPI, File, Form, HTTPException, Response,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.staticfiles import StaticFiles

from ..gen.schema import load_case
from ..gen.units import CaseError
from ..geometry.loaders import GeometryError
from ..geometry.pipeline import build_tunnel_case
from .jobs import (CASES, BusyError, JobManager, NotFound, case_dir,
                   device_info, estimated_seconds, read_field, read_forces,
                   read_q, snapshots)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    jobs.attach_loop(asyncio.get_running_loop())
    yield
    jobs.shutdown()


app = FastAPI(title="fluidfront", version="0.0.1", lifespan=lifespan)
jobs = JobManager()


def _safe_name(raw: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-_")[:40] or "case"
    if not re.match(r"^[a-z0-9]", s):
        s = "c-" + s
    n, i = s, 2
    while (CASES / n).exists():
        n, i = f"{s}-{i}", i + 1
    return n


# ---------------------------------------------------------------- topology --

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "stage": "M4-server", "active_case": jobs.active}


@app.get("/api/device")
def device() -> dict:
    return device_info()


# ------------------------------------------------------------------ cases --

@app.get("/api/cases")
def list_cases() -> list[dict]:
    return jobs.list_cases()


@app.post("/api/cases")
async def create_case(
    model: UploadFile = File(...),
    name: str = Form(""),
    u_si: float = Form(...),                 # flow speed, m/s
    tier: str = Form("standard"),            # preview | standard | detailed
    fluid: str = Form("air"),
    rotation_deg: str = Form("90,0,0"),      # STL Z-up -> tunnel +y default
    size_lu: int = Form(0),                  # >0 overrides quality tier
    model_units: str = Form("m"),            # m | cm | mm | in (scale of the CAD file)
    precision: str = Form("fp16s"),
) -> dict:
    scale_si = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254}.get(model_units)
    if scale_si is None:
        raise HTTPException(status_code=422,
                            detail=f"model_units {model_units!r} not in m/cm/mm/in")
    stem = Path(model.filename or "model").stem
    cname = _safe_name(name or stem)
    cdir = CASES / cname
    cdir.mkdir(parents=True, exist_ok=True)
    suffix = (Path(model.filename or "").suffix or ".stl").lower()
    upload = cdir / f"upload{suffix}"
    upload.write_bytes(await model.read())

    dev = device_info()
    # cap the mesh so it fits in ~75% of free VRAM (FP16S D3Q19 ~55 B/cell),
    # AND inside one OpenCL buffer (driver max-memory-allocation cap; measured
    # 955 MB single buffer on a 4 GB card -> the binding limit, not VRAM)
    max_cells = min(int(dev["free_mi"] * 0.75 * 1024 * 1024 // 55),
                    int(dev["maxbuf_mi"] * 0.90 * 1024 * 1024 // 38))
    try:
        rot = tuple(float(v) for v in rotation_deg.replace(";", ",").split(","))
        case, report = build_tunnel_case(
            upload, cdir, u_si=u_si, tier=tier, fluid=fluid, rotation_deg=rot,
            size_lu=size_lu or None, max_cells=max_cells,
            scale_si=scale_si, precision=precision)
        (cdir / "case.json").write_text(json.dumps(case, indent=2))
        spec = load_case(cdir)  # guard rails fire here (tau, u_lu, ...)
    except (GeometryError, CaseError, ValueError) as e:
        shutil.rmtree(cdir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001 - model files bring surprise formats
        shutil.rmtree(cdir, ignore_errors=True)
        raise HTTPException(status_code=422,
                            detail=f"could not process this model: {type(e).__name__}: {e}")

    rep = {"warnings": report["warnings"], "stats": report["stats"],
           "blockage": report["blockage"],
           "length_si": case["flow"]["length_si"],
           "domain": report["layout"].as_domain(),
           "size_lu": report["layout"].size_lu,
           "cells": report["layout"].nx * report["layout"].ny * report["layout"].nz,
           "t_steps": case["run"]["t_steps"]}
    rep["estimate_s"] = estimated_seconds(rep["cells"], rep["t_steps"], dev,
                                          subgrid=spec.subgrid)
    (cdir / "report.json").write_text(json.dumps(rep, indent=2))
    jobs.write_status(cname, state="created", percent=0)

    d = spec.derived
    return {"name": cname, "case": case, "report": rep,
            "derived": {"re_si": d.re_si, "re_lu": d.re_lu, "nu_lu": d.nu_lu,
                        "tau": d.tau, "unit_m": d.unit_m,
                        "subgrid": spec.subgrid, "warnings": d.warnings}}


@app.get("/api/cases/{name}")
def case_detail(name: str) -> dict:
    try:
        cdir = CASES / name
        if not (cdir / "case.json").is_file():
            raise NotFound(name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"no such case: {name!r}")
    detail = {"name": name, "case": json.loads((cdir / "case.json").read_text()),
              "status": jobs.read_status(name)}
    for extra in ("report.json",):
        if (cdir / extra).is_file():
            detail[extra[:-5]] = json.loads((cdir / extra).read_text())
    return detail


@app.delete("/api/cases/{name}")
def delete_case(name: str) -> dict:
    try:
        jobs.delete(name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"no such case: {name!r}")
    except BusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"deleted": name}


# -------------------------------------------------------------- run control -

@app.post("/api/cases/{name}/run")
def run_case(name: str) -> dict:
    try:
        jobs.start(name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"no such case: {name!r}")
    except BusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"started": name}


@app.post("/api/cases/{name}/stop")
def stop_case(name: str) -> dict:
    try:
        jobs.stop(name)
    except NotFound:
        raise HTTPException(status_code=404,
                            detail=f"case {name!r} is not running")
    return {"stopping": name}


# ------------------------------------------------------------ live results -

@app.get("/api/cases/{name}/forces")
def forces(name: str) -> dict:
    try:
        data = read_forces(name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"no such case: {name!r}")
    if data is None:
        raise HTTPException(status_code=404, detail="no forces yet (run first)")
    return data


@app.get("/api/cases/{name}/snapshots")
def snapshot_list(name: str) -> dict:
    try:
        return snapshots(name)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"no such case: {name!r}")


@app.get("/api/cases/{name}/field")
def field(name: str, which: str = "u", t: int | None = None,
          max_points: int = 200_000, fmt: str = "json"):
    try:
        arr, meta = read_field(name, which, t, max_points=min(max_points, 4_000_000))
    except NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    nx, ny, nz, ncomp, t_actual = meta
    info = {"which": which, "dims": [nx, ny, nz], "ncomp": ncomp,
            "t": t_actual, "dtype": "float32-le"}
    if fmt == "bin":  # raw float32 + X-Meta header: ~25% lighter than base64
        return Response(content=arr.tobytes(),
                        media_type="application/octet-stream",
                        headers={"X-Meta": json.dumps(info)})
    return {**info, "data_b64": base64.b64encode(arr.tobytes()).decode()}


@app.get("/api/cases/{name}/stamp")
def stamp(name: str) -> dict:
    """Cheap identity of a case's setup: changes whenever it is re-created
    (new rotation / quality), so the viewer knows to rebuild the model."""
    src = case_dir(name) / "gen" / "setup.cpp"
    return {"stamp": int(src.stat().st_mtime)}


@app.get("/api/cases/{name}/q")
def q_field(name: str, t: int | None = None, grid: int = 96,
            fmt: str = "json"):
    """Q-criterion vortex grid (uniform cube, physical 1/s^2) for the
    iso-surface view. iso_lo/iso_hi bracket the slider's iso-level range."""
    try:
        res, t_actual = read_q(name, t, grid=max(32, min(128, grid)))
    except NotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    info = {"dims": list(res["dims"]), "full_dims": list(res["full_dims"]),
            "t": t_actual,
            "iso_lo": res["iso_lo"], "iso_hi": res["iso_hi"],
            "dtype": "float32-le"}
    if fmt == "bin":
        return Response(content=res["q"].tobytes(),
                        media_type="application/octet-stream",
                        headers={"X-Meta": json.dumps(info)})
    return {**info, "data_b64": base64.b64encode(res["q"].tobytes()).decode()}


# ------------------------------------------------------------------- events -

@app.websocket("/ws")
async def ws_events(ws: WebSocket) -> None:
    await ws.accept()
    q = jobs.subscribe()
    try:
        await ws.send_json({"kind": "hello", "cases": jobs.list_cases(),
                            "active": jobs.active, "device": device_info()})
        while True:
            await ws.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        jobs.unsubscribe(q)


# ---------------------------------------------------------------- web app --
# The WebUI is plain ES modules served from the same origin (no build step,
# no CORS); three.js is vendored under web/vendor/. Mounted LAST so /api and
# /ws keep priority.

WEB = Path(__file__).resolve().parents[2] / "web"
if WEB.is_dir():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
