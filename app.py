import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

os.environ.setdefault("LD_LIBRARY_PATH", "/usr/local/lib")

import camera
import network

log = logging.getLogger(__name__)


class SettingValue(BaseModel):
    value: str | int | float | bool


class HomeNetworkRequest(BaseModel):
    ssid: str
    password: str


def _try_connect(app: FastAPI) -> None:
    try:
        app.state.camera = camera.connect()
        log.info("camera connected: %s", app.state.camera.model)
        app.state.camera_warned = False
    except Exception as exc:
        app.state.camera = None
        if not app.state.camera_warned:
            log.warning("camera connect failed: %r", exc)
            app.state.camera_warned = True
        else:
            log.debug("camera connect still failing: %r", exc)


CAMERA_POLL_INTERVAL = 3.0
_connect_lock = asyncio.Lock()

NETWORK_SWITCH_DELAY = 1.5  # let the HTTP response flush before the radio moves
_network_lock = asyncio.Lock()


async def _connect_if_needed(app: FastAPI) -> None:
    async with _connect_lock:
        if app.state.camera is None:
            await run_in_threadpool(_try_connect, app)


async def _camera_watcher(app: FastAPI) -> None:
    while True:
        await _connect_if_needed(app)
        await asyncio.sleep(CAMERA_POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.camera = None
    app.state.camera_warned = False
    _try_connect(app)
    watcher = asyncio.create_task(_camera_watcher(app))
    yield
    watcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watcher
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
    await _connect_if_needed(app)
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
        log.warning("set_setting %s=%r failed: %r", name, body.value, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return await run_in_threadpool(cam.list_settings)


@app.get("/api/network/status")
async def network_status():
    return await run_in_threadpool(network.status)


async def _deferred_connect_home(ssid: str, password: str) -> None:
    async with _network_lock:
        await asyncio.sleep(NETWORK_SWITCH_DELAY)
        await run_in_threadpool(network.connect_home, ssid, password)


async def _deferred_connect_ap() -> None:
    async with _network_lock:
        await asyncio.sleep(NETWORK_SWITCH_DELAY)
        await run_in_threadpool(network.connect_ap)


@app.post("/api/network/home")
async def network_home(body: HomeNetworkRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_deferred_connect_home, body.ssid, body.password)
    return {"ok": True, "switching_to": body.ssid}


@app.post("/api/network/ap")
async def network_ap(background_tasks: BackgroundTasks):
    background_tasks.add_task(_deferred_connect_ap)
    return {"ok": True, "switching_to": network.AP_CONN}


app.mount("/", StaticFiles(directory="web", html=True), name="web")