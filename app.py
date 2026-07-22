import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

os.environ.setdefault("LD_LIBRARY_PATH", "/usr/local/lib")

import camera

log = logging.getLogger(__name__)


class SettingValue(BaseModel):
    value: str | int | float | bool


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
        if app.state.camera.recording:
            try:
                app.state.camera.set_recording(False)
            except Exception:
                pass
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


async def _drop_camera(exc):
    old = app.state.camera
    app.state.camera = None
    app.state.camera_warned = False
    if old is None:
        return
    log.warning("camera connection lost (%r); dropping — watcher will reconnect", exc)
    with contextlib.suppress(Exception):
        await run_in_threadpool(camera.disconnect, old)


async def _run_camera(method, *args):
    try:
        return await run_in_threadpool(method, *args)
    except Exception as exc:
        if camera.is_disconnect_error(exc):
            await _drop_camera(exc)
            raise HTTPException(status_code=503, detail="camera disconnected") from exc
        raise


@app.get("/api/status")
async def status():
    cam = app.state.camera
    return {
        "connected": cam is not None,
        "model": cam.model if cam else None,
        "recording": cam.recording if cam else False,
    }


@app.post("/api/connect")
async def connect():
    await _connect_if_needed(app)
    if app.state.camera is None:
        raise HTTPException(status_code=503, detail="no camera found")
    return {"connected": True, "model": app.state.camera.model}


@app.post("/api/capture")
async def capture():
    cam = _require_camera()
    try:
        path = await _run_camera(cam.capture)
    except RuntimeError as exc:  # e.g. recording in progress
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "path": path}


LIVEVIEW_BOUNDARY = "pathfinderframe"
LIVEVIEW_FRAME_INTERVAL = 1 / 30


@app.get("/api/liveview")
async def liveview(request: Request):
    _require_camera()

    async def frames():
        while not await request.is_disconnected():
            cam = app.state.camera
            if cam is None:
                break
            try:
                jpeg = await _run_camera(cam.preview)
            except HTTPException:
                break  # camera dropped (503); the watcher will rebuild it
            except Exception as exc:
                log.debug("liveview frame failed: %r", exc)
                await asyncio.sleep(0.3)
                continue
            yield (
                b"--" + LIVEVIEW_BOUNDARY.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                + jpeg + b"\r\n"
            )
            await asyncio.sleep(LIVEVIEW_FRAME_INTERVAL)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={LIVEVIEW_BOUNDARY}",
    )


async def _set_recording(on: bool):
    cam = _require_camera()
    try:
        recording = await _run_camera(cam.set_recording, on)
    except HTTPException:
        raise  # disconnect (503) — don't mask it as a 400
    except Exception as exc:
        log.warning("set_recording(%s) failed: %r", on, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "recording": recording}


@app.post("/api/record/start")
async def record_start():
    return await _set_recording(True)


@app.post("/api/record/stop")
async def record_stop():
    return await _set_recording(False)


@app.get("/api/settings")
async def get_settings():
    cam = _require_camera()
    return await _run_camera(cam.list_settings)


@app.post("/api/settings/{name}")
async def set_setting(name: str, body: SettingValue):
    cam = _require_camera()
    try:
        await _run_camera(cam.set_setting, name, body.value)
    except HTTPException:
        raise  # disconnect (503) — don't mask it as a 400
    except Exception as exc:
        log.warning("set_setting %s=%r failed: %r", name, body.value, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return await _run_camera(cam.list_settings)


app.mount("/", StaticFiles(directory="web", html=True), name="web")