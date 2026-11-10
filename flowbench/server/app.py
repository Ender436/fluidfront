"""Flowbench app server (M0 stub).

Serves the API used by the WebUI. Real case management arrives in M4;
for M0 this proves the local serve loop (make serve -> :8123).
"""

from fastapi import FastAPI

app = FastAPI(title="flowbench", version="0.0.1")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "stage": "M0-scaffold"}
