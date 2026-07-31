import asyncio
import contextlib
import logging
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

os.environ.setdefault("LD_LIBRARY_PATH", "/usr/local/lib")

import camera

log = logging.getLogger(__name__)

DEFAULT_MAX_BULB_SECONDS = 900.0
MAX_FOCUS_STEPS = 10000


def _max_bulb_seconds() -> float:
    raw = os.environ.get("PATHFINDER_MAX_BULB_SECONDS")
    if raw is None:
        return DEFAULT_MAX_BULB_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not 0 < value < float("inf"):
        log.warning("ignoring invalid PATHFINDER_MAX_BULB_SECONDS=%r; using %.0fs",
                    raw, DEFAULT_MAX_BULB_SECONDS)
        return DEFAULT_MAX_BULB_SECONDS
    return value


MAX_BULB_SECONDS = _max_bulb_seconds()


class SettingValue(BaseModel):
    value: str | int | float | bool


class FocusStep(BaseModel):
    steps: int = Field(ge=-MAX_FOCUS_STEPS, le=MAX_FOCUS_STEPS)


class BulbExposure(BaseModel):
    seconds: float = Field(gt=0, le=MAX_BULB_SECONDS, allow_inf_nan=False)


class AfPoint(BaseModel):
    x: float
    y: float


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
CONNECT_TIMEOUT = 5.0
_connect_lock = asyncio.Lock()


async def _connect_if_needed(app: FastAPI) -> bool:
    try:
        await asyncio.wait_for(_connect_lock.acquire(), CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("a camera connect attempt has been in flight for over %.0fs "
                    "— not queueing another behind it", CONNECT_TIMEOUT)
        return False
    try:
        if app.state.camera is None:
            await run_in_threadpool(_try_connect, app)
    finally:
        _connect_lock.release()
    return True


async def _camera_watcher(app: FastAPI) -> None:
    while True:
        await _connect_if_needed(app)
        await asyncio.sleep(CAMERA_POLL_INTERVAL)


def _sd_notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as exc:
        log.debug("sd_notify(%r) failed: %r", message, exc)
        return False
    return True


def _watchdog_interval() -> float | None:
    raw = os.environ.get("WATCHDOG_USEC")
    owner = os.environ.get("WATCHDOG_PID")
    if not raw or (owner and owner != str(os.getpid())):
        return None
    try:
        usec = int(raw)
    except ValueError:
        log.warning("ignoring malformed WATCHDOG_USEC=%r", raw)
        return None
    if usec <= 0:
        return None
    return usec / 2_000_000.0                # ping at half the deadline


async def _pool_probe() -> None:
    await run_in_threadpool(lambda: None)


async def _watchdog(interval: float) -> None:
    probe = None
    try:
        while True:
            if probe is None:
                probe = asyncio.create_task(_pool_probe())
            await asyncio.sleep(interval)
            if not probe.done():
                log.error("threadpool has not answered a liveness probe in %.0fs "
                          "— withholding the systemd watchdog ping", interval)
                continue
            if probe.exception() is not None:
                log.warning("threadpool liveness probe raised: %r", probe.exception())
            probe = None
            _sd_notify("WATCHDOG=1")
    finally:
        if probe is not None:
            probe.cancel()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.camera = None
    app.state.camera_warned = False
    _try_connect(app)
    tasks = [asyncio.create_task(_camera_watcher(app))]
    interval = _watchdog_interval()
    if interval is None:
        log.info("no systemd watchdog configured (WATCHDOG_USEC unset) — a hung "
                 "process will not be restarted for us")
    else:
        _sd_notify("READY=1")
        tasks.append(asyncio.create_task(_watchdog(interval)))
        log.info("systemd watchdog armed; pinging every %.0fs", interval)
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
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
    except camera.CameraBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    attempted = await _connect_if_needed(app)
    if app.state.camera is None:
        raise HTTPException(status_code=503, detail=(
            "no camera found" if attempted
            else "a camera connect attempt is already in flight"))
    return {"connected": True, "model": app.state.camera.model}


@app.post("/api/capture")
async def capture():
    cam = _require_camera()
    try:
        path = await _run_camera(cam.capture)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "path": path}


@app.post("/api/bulb")
async def bulb(body: BulbExposure):
    cam = _require_camera()
    try:
        path = await _run_camera(cam.bulb, body.seconds)
    except HTTPException:
        raise  # disconnect (503)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        log.warning("bulb(%s) failed: %r", body.seconds, exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "path": path}


LIVEVIEW_BOUNDARY = "pathfinderframe"
LIVEVIEW_FRAME_INTERVAL = 1 / 30
LIVEVIEW_RETRY_INTERVAL = 0.3


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
            except HTTPException as exc:
                if exc.status_code != 409:
                    break  # camera dropped (503); the watcher will rebuild it
                log.debug("liveview frame skipped: %s", exc.detail)
                await asyncio.sleep(LIVEVIEW_RETRY_INTERVAL)
                continue
            except Exception as exc:
                log.debug("liveview frame failed: %r", exc)
                await asyncio.sleep(LIVEVIEW_RETRY_INTERVAL)
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
        raise  # disconnect (503)
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


@app.post("/api/autofocus")
async def autofocus():
    cam = _require_camera()
    try:
        mode = await _run_camera(cam.autofocus)
    except HTTPException:
        raise  # disconnect (503)
    except Exception as exc:
        log.warning("autofocus failed: %r", exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "focusmode": mode}


@app.post("/api/focus")
async def manual_focus(body: FocusStep):
    cam = _require_camera()
    try:
        mode = await _run_camera(cam.manual_focus, body.steps)
    except HTTPException:
        raise  # disconnect (503)
    except Exception as exc:
        log.warning("manual_focus(%d) failed: %r", body.steps, exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "focusmode": mode}


@app.post("/api/afpoint")
async def af_point(body: AfPoint):
    cam = _require_camera()
    try:
        await _run_camera(cam.set_af_point, body.x, body.y)
    except HTTPException:
        raise  # disconnect (503)
    except Exception as exc:
        log.warning("set_af_point(%s, %s) failed: %r", body.x, body.y, exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.get("/api/telemetry")
async def telemetry():
    cam = _require_camera()
    return await _run_camera(cam.telemetry)


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
        raise  # disconnect (503)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no settable setting named {name!r}")
    except Exception as exc:
        log.warning("set_setting %s=%r failed: %r", name, body.value, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    return await _run_camera(cam.list_settings)


app.mount("/", StaticFiles(directory="web", html=True), name="web")