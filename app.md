# Backend Architecture

Pathfinder's backend is a single-process FastAPI app that bridges a browser UI to a
tethered camera over `libgphoto2`. It's small on purpose: one camera, one client-ish
usage pattern (a phone on the Pi's own WiFi AP), no database, no auth.

## Process & entry point

- **`run.py`** — process entry point. Calls `log.configure_logging()` before
  anything else so import-time log lines from `app`/`camera` are captured, then runs
  `app:app` under `uvicorn` on `0.0.0.0:8080`. Started on boot by the `pathfinder`
  systemd unit (installed by `setup.sh`).
- **`log.py`** — stdlib `logging.basicConfig` wrapper. Level comes from
  `PATHFINDER_LOG_LEVEL` (default `DEBUG`).
- **`app.py`** — the FastAPI app: HTTP routes, camera lifecycle management, and static
  file serving for `web/`.

## Camera lifecycle

The app holds at most one camera connection at a time, stored on `app.state.camera`
(`None` when disconnected). There's no request-scoped connection — every route reads
the same shared instance.

```
lifespan startup
  ├─ app.state.camera = None
  ├─ _try_connect(app)              # synchronous first attempt, blocks startup briefly
  └─ spawn _camera_watcher(app)     # background asyncio task

_camera_watcher loop (every CAMERA_POLL_INTERVAL = 3s)
  └─ _connect_if_needed(app)
       └─ if app.state.camera is None:
            run_in_threadpool(_try_connect, app)   # libgphoto2 calls are blocking

lifespan shutdown
  ├─ cancel watcher task, await its cancellation
  └─ camera.disconnect(app.state.camera)  if connected
```

Key points:
- `_try_connect` swallows all exceptions (camera absent, USB error, etc.) and just
  logs — once at `WARNING` when the failure starts, then `DEBUG` on repeats
  (`app.state.camera_warned` flag), so a camera left unplugged doesn't spam the log
  every 3 seconds.
- `_connect_lock` (an `asyncio.Lock`) serializes connection attempts so the poll loop
  and an explicit `POST /api/connect` can't race each other into calling
  `gp.Camera().init()` concurrently.
- All actual `gphoto2` calls run via `run_in_threadpool` — the binding is a blocking
  C extension, so keeping it off the event loop is what keeps `/api/status` polling
  and the settings UI responsive while a capture or reconnect is in flight.
- If the camera disappears mid-session (unplugged, or a Sony body re-enumerating
  on the USB bus mid-capture), the app doesn't *proactively* detect it —
  `app.state.camera` stays set until the next operation on it raises. But that
  failing operation now triggers recovery: every hardware route runs through
  `_run_camera()`, which inspects the exception via `camera.is_disconnect_error()`
  — true for transport-level gphoto2 codes (`GP_ERROR_IO` -7, `GP_ERROR_IO_USB_FIND`
  -52, `GP_ERROR_IO_USB_CLAIM` -53, and other I/O codes) that mean the USB handle
  is dead rather than the request being bad. On such an error `_drop_camera()` nulls
  `app.state.camera` (after best-effort `close()`ing the stale handle to release the
  USB claim) and the caller gets a **503**. The `_camera_watcher` loop then re-inits
  a fresh connection within `CAMERA_POLL_INTERVAL` (≤3s), which re-resolves the
  camera's current USB address. Without this, a stale handle would fail *identically*
  on every subsequent call forever (the watcher only reconnects when state is `None`),
  which is exactly the failure captured in early field logs. A hard disconnect still
  surfaces on the *next* API call as a one-off 503, not instantly — but it now
  self-heals instead of wedging the connection.

  Logical errors (a bad setting value, a capture refused because recording is in
  progress) are *not* disconnect errors: they propagate as 400/409 and leave the
  connection intact.

## HTTP API

All routes are declared directly on the `FastAPI()` instance in `app.py` (no routers
— the surface is small enough that splitting it out would be premature).

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/status` | Returns `{connected, model, recording}` from current `app.state.camera`. Never touches hardware — just reads state (including the `recording` flag). |
| `POST` | `/api/connect` | Forces a connection attempt via `_connect_if_needed` (reuses the same lock as the watcher). 503 if still no camera after trying. |
| `POST` | `/api/capture` | 503 via `_require_camera()` if disconnected. Otherwise runs `cam.capture()` in a threadpool and returns the saved file path. Returns 409 if a recording is in progress (stills and video are mutually exclusive on the body). |
| `GET` | `/api/liveview` | 503 if disconnected. Otherwise streams `multipart/x-mixed-replace` — a continuous MJPEG feed, one `cam.preview()` frame per part, that an `<img>` decodes in place. Each frame is a separate `_run_camera(cam.preview)` call, so it grabs the camera lock, pulls one frame, and releases, letting capture/record/settings interleave between frames. The generator stops when the client disconnects (`request.is_disconnected()`), when the camera drops (a 503 from `_run_camera` breaks the loop and the watcher rebuilds it), or paced by `LIVEVIEW_FRAME_INTERVAL`. |
| `POST` | `/api/record/start` | 503 if disconnected. Sets the vendor's movie toggle widget on (`cam.set_recording(True)`), returns `{ok, recording}`. Idempotent — a no-op if already recording. 400 if the body exposes no movie widget. |
| `POST` | `/api/record/stop` | Mirror of the above with the toggle off. Idempotent — a no-op if not recording. |
| `GET` | `/api/settings` | Returns the camera's current writable settings as a list of widget descriptors (see below). |
| `POST` | `/api/settings/{name}` | Body `{value}` (str/int/float/bool). Sets one setting; on failure returns 400 with the underlying error; on success re-reads and returns the full settings list so the UI can pick up any settings that changed as a side effect (e.g. aperture limits shifting with ISO). |

`_require_camera()` is the single guard used by every route that needs hardware —
raises `HTTPException(503, "no camera connected")` if `app.state.camera is None`.

`app.mount("/", StaticFiles(directory="web", html=True))` is registered **last**, so
it acts as a catch-all serving `web/index.html` and assets after the `/api/*` routes
have had first refusal.

## `camera/` package — the gphoto2 boundary

`app.py` never touches the `gphoto2` binding directly; everything goes through
`camera/`.

- **`camera/__init__.py`** — public surface: re-exports `connect()` / `disconnect()`
  from `gp2`. This is the only import `app.py` makes (`import camera`).
- **`camera/gp2.py`** — the actual libgphoto2 backend.
  - `connect()` — `gp.Camera().init()`, reads the model string off
    `get_abilities()`, wraps it in a `Gphoto2Camera`.
  - `Gphoto2Camera` — one instance per physical connection. Holds a
    `threading.Lock` (`_lock`) guarding every hardware operation, since
    `run_in_threadpool` calls execute on worker threads and could otherwise overlap
    (e.g. a capture racing a settings write).
  - `preview()` — grabs one liveview JPEG via `capture_preview()`; the driver
    behind `/api/liveview`. Runs under `_lock` and refuses while recording.
  - `set_recording(on)` — starts/stops movie recording by writing the vendor's
    movie toggle widget (`_quirks["movie_widget"]`, default `"movie"`) with
    `set_config()`. Guarded by `_lock`; idempotent (compares against the tracked
    `recording` flag and no-ops if already in the requested state). The `recording`
    attribute is written here under the lock but read lock-free by `/api/status` —
    safe because a lone bool read/write is atomic under the GIL, so only the
    check-then-act needs the lock. The movie widget lives in gphoto2's `actions`
    section, which `INCLUDE_SECTIONS` excludes, so it never appears as a settings row.
  - `capture()`:
    1. Enforces `shot_gap` — a per-model minimum interval since the last shot,
       sleeping out the remainder if called too soon.
    2. `_drain_events()` — flushes any pending camera events before shooting, since
       stale queued events can interfere with the capture call on some bodies.
    3. `_capture_with_retry()` — retries up to `capture_retry_attempts` times on
       `GP_ERROR`, with a 1s backoff and another event drain between attempts.
    4. Downloads the resulting file to `CAPTURE_DIR` (env
       `PATHFINDER_CAPTURE_DIR`, default `captures/`), prefixed with a unix
       timestamp to avoid collisions.
  - `list_settings()` — walks the camera's config tree (`get_config()`), descending
    into `WINDOW`/`SECTION` nodes (`_walk`), keeping only widgets under
    `INCLUDE_SECTIONS = {imgsettings, capturesettings, settings}` whose type is one
    Pathfinder knows how to render (`_KIND`: radio/menu → `choice`, toggle →
    `toggle`, range → `range`, text → `text`) and that aren't read-only. This is
    what makes the UI camera-agnostic — whatever widgets the connected body exposes
    in those sections become the settings panel.
  - `set_setting(name, value)` — looks up the widget by name, coerces `value` to the
    type gphoto2 expects for that widget (`_coerce`: range → `float`, toggle →
    `int`, else `str`), writes it back with `set_config()`.
- **`camera/sony.py`** — per-model quirk table. `quirks(model)` returns
  `{shot_gap, capture_retry_attempts}` merged from a `GENERAL` Sony default and any
  model-specific override in `MODELS` (currently only `ILCE-7M4` / α7 IV, with no
  overrides beyond the general Sony numbers). `gp2._quirks_for(model)` checks each
  module in `VENDORS = [sony]` in order and falls back to
  `DEFAULT_QUIRKS = {shot_gap: 0.0, capture_retry_attempts: 1}` for anything
  unrecognized. Adding a new vendor means adding a module with a `quirks(model)`
  function and listing it in `VENDORS`.

## Concurrency model, summarized

Two independent forms of serialization protect the single camera object:

1. `asyncio.Lock` (`_connect_lock` in `app.py`) — serializes *connection attempts*
   (watcher vs. explicit `/api/connect`).
2. `threading.Lock` (`Gphoto2Camera._lock` in `gp2.py`) — serializes *operations on
   an already-connected camera* (capture vs. settings read/write), since those run
   as separate threadpool calls that could otherwise interleave.

There is no request queue beyond these locks — concurrent requests block on the
relevant lock rather than being rejected, so a slow capture will make a concurrent
`/api/settings` call wait rather than fail.

## Frontend contract (for context)

`web/script.js` polls `/api/status`, and when connected, renders `/api/settings`'s
widget list dynamically (choice/toggle/range/text controls) and posts edits to
`/api/settings/{name}`. It has no camera-specific logic — everything it needs to
render a control comes from the widget descriptor shape defined in `gp2._describe`.
