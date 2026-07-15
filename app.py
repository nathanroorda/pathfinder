import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

os.environ.setdefault("LD_LIBRARY_PATH", "/usr/local/lib")

import camera

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pathfinder")


class SettingValue(BaseModel):
    value: str | int | float | bool


def _try_connect(app: FastAPI) -> None:
    try:
        app.state.camera = camera.connect()
        log.info("camera connected: %s", app.state.camera.model)
    except Exception as exc:
        app.state.camera = None
        log.warning("camera connect failed: %r", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.camera = None
    _try_connect(app)
    yield
    if app.state.camera is not None:
        try:
            camera.disconnect(app.state.camera)
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


def _require_camera():
    cam = app.state.camera
    if cam is None:
        raise HTTPException(status_code=503, detail="no camera connected")
    return cam


@app.get("/api/status")
async def status():
    cam = app.state.camera
    return {"connected": cam is not None, "model": cam.model if cam else None}


@app.post("/api/connect")
async def connect():
    if app.state.camera is None:
        await run_in_threadpool(_try_connect, app)
    if app.state.camera is None:
        raise HTTPException(status_code=503, detail="no camera found")
    return {"connected": True, "model": app.state.camera.model}


@app.post("/api/capture")
async def capture():
    cam = _require_camera()
    path = await run_in_threadpool(cam.capture)
    return {"ok": True, "path": path}


@app.get("/api/settings")
async def get_settings():
    cam = _require_camera()
    return await run_in_threadpool(cam.list_settings)


@app.post("/api/settings/{name}")
async def set_setting(name: str, body: SettingValue):
    cam = _require_camera()
    try:
        await run_in_threadpool(cam.set_setting, name, body.value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await run_in_threadpool(cam.list_settings)


app.mount("/", StaticFiles(directory="web", html=True), name="web")