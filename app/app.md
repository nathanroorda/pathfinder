# Backend Architecture

Pathfinder's backend is a single-process FastAPI app that bridges a browser UI to a
tethered camera over `libgphoto2`. It's small on purpose: one camera, one client-ish
usage pattern (a phone on the Pi's own WiFi AP), no database, no auth.

## Process & entry point

- **`tools/run.py`** — process entry point. It lives in `tools/`, which means
  `sys.path[0]` is `tools/` rather than the project root, so the first thing it
  does is put the root on `sys.path`; without that, `from logs import ...` and
  `from app.app import ...` both fail with `ModuleNotFoundError` no matter what
  the working directory is (Python adds the *script's* directory to the path,
  not the cwd). It then calls `logs.configure_logging()` before anything
  else so import-time log lines from `app`/`camera` are captured, then hands the
  imported `app` object to `uvicorn` on `0.0.0.0:8080` — it passes the object,
  not an `"app.app:app"` import string, so there is no second import path to keep
  in sync. Started on boot by the `pathfinder` systemd unit (installed by
  `tools/setup.sh`), which also arms the watchdog — see "Timeouts and the
  watchdog".
- **`logs/`** — stdlib `logging.basicConfig` wrapper. Level comes from
  `PATHFINDER_LOG_LEVEL` (default `DEBUG`). `configure_logging` is re-exported
  from the package, so the import stays `from logs import configure_logging`
  and must remain the first thing `tools/run.py` does after the `sys.path` bootstrap —
  before `app`/`camera` are imported, or their import-time records are lost.
- **`app/app.py`** — the FastAPI app: HTTP routes, camera lifecycle management, and static
  file serving for `web/`.

### Why `app/__init__.py` is empty

`camera/` and `logs/` both re-export a curated public surface from `__init__.py`.
`app/` deliberately does not, and the reason is a name collision worth knowing
about: the package is `app` and so is the module inside it, so
`from .app import app` in `__init__.py` binds the *FastAPI instance* to the
package attribute `app` — which then shadows the submodule. `import app.app`
afterwards yields the FastAPI object rather than the module, and every test that
reaches for an internal (`app_module._drop_camera`, `app_module.MAX_FOCUS_STEPS`)
fails with `AttributeError`.

So imports name the module explicitly:

```python
from app.app import app          # tools/run.py
import app.app as app_module     # tests
```

The trap to avoid is writing `from app import app` with an empty `__init__.py`.
That does **not** raise — Python falls back to importing the submodule, so the
name binds to the *module* and `uvicorn.run()` fails later with a confusing
error instead of at import. The 31 attributes the suite reaches for are listed
implicitly by `tests/test_app_routes.py`; they include private helpers, which is
why a curated re-export could not serve them even without the collision.

`StaticFiles(directory="web")` at the bottom of `app/app.py` resolves against the
**working directory**, not the file, so it is unaffected by this move — but it
does mean the process must still be started from the repo root. The systemd unit
sets `WorkingDirectory=$PROJECT_DIR`, and `tests/support.py` chdirs there for the
same reason.

## Camera lifecycle

The app holds at most one camera connection at a time, stored on `app.state.camera`
(`None` when disconnected). There's no request-scoped connection — every route reads
the same shared instance.

```
lifespan startup
  ├─ app.state.camera = None
  ├─ _try_connect(app)              # synchronous first attempt, blocks startup briefly
  ├─ spawn _camera_watcher(app)     # background asyncio task
  └─ spawn _watchdog(interval)      # only if systemd asked for one

_camera_watcher loop (every CAMERA_POLL_INTERVAL = 3s)
  └─ _connect_if_needed(app)
       ├─ acquire _connect_lock, bounded by CONNECT_TIMEOUT (5s) → False if not
       └─ if app.state.camera is None:
            run_in_threadpool(_try_connect, app)   # libgphoto2 calls are blocking

lifespan shutdown
  ├─ cancel both tasks, await their cancellation
  └─ camera.disconnect(app.state.camera)  if connected
```

Key points:
- `_try_connect` swallows all exceptions (camera absent, USB error, etc.) and just
  logs — once at `WARNING` when the failure starts, then `DEBUG` on repeats
  (`app.state.camera_warned` flag), so a camera left unplugged doesn't spam the log
  every 3 seconds.
- `_connect_lock` (an `asyncio.Lock`) serializes connection attempts so the poll loop
  and an explicit `POST /api/connect` can't race each other into calling
  `gp.Camera().init()` concurrently. It is acquired with a `CONNECT_TIMEOUT` (5s)
  bound: the handshake it protects is a blocking USB call that can wedge, and an
  unbounded `async with` would hang the watcher and every `/api/connect` behind it
  forever. On expiry `_connect_if_needed` returns `False` (the route answers 503
  "a camera connect attempt is already in flight") and the watcher simply tries
  again on its next tick.
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
  progress, a setting name outside the allowlist) are *not* disconnect errors:
  they propagate as 400/404/409 and leave the connection intact.

- A `camera.CameraBusy` — the camera lock could not be taken within its deadline
  — is mapped by `_run_camera` to a **409** before the disconnect check, since
  nothing was sent to the body and the connection is healthy. See "Timeouts and
  the watchdog" below for why the bus lock has a deadline at all.

## Timeouts and the watchdog

Every `gphoto2` call is a blocking C call that **cannot be interrupted from
Python** — there is no timeout to pass and no way to cancel one in flight. A body
that wedges mid-PTP-transaction (Sony bodies demonstrably do; the `-52`
self-healing exists because of it) therefore keeps its threadpool worker forever.
Two bounds and one backstop keep that from taking the process down with it:

1. **The camera lock is acquired with a deadline** (`camera/gp2.py`'s `_bus`),
   so requests queued behind a stuck operation are refused with 409 instead of
   parking one more of the 40 `run_in_threadpool` workers each. Documented in
   `camera.md`.
2. **`_connect_lock` is acquired with a deadline**, as above.
3. **A systemd watchdog** turns "hung forever" into "restarted in ~30s", which is
   the only recovery left once a worker is genuinely stuck in libgphoto2. The
   unit sets `WatchdogSec=30`, `NotifyAccess=main` and `Restart=always` —
   `on-failure` cannot help, because a hung process has not failed: it is alive
   and doing nothing.

`_watchdog(interval)` pings `WATCHDOG=1` to `$NOTIFY_SOCKET` every
`WATCHDOG_USEC / 2` (`_sd_notify` speaks the protocol directly — it is one
datagram on a unix socket, which is cheaper than depending on `python-systemd`).
If systemd didn't ask for a watchdog (`WATCHDOG_USEC` unset, or set for a
different PID) the task is never started and the app logs that it is unguarded.

The important detail is **what earns a ping**. A bare `while True: ping` would
keep the unit looking healthy through the exact failure this guards against: the
event loop is fine — it is the worker pool that is gone. So each interval must be
paid for by `_pool_probe()`, a round trip through the same threadpool the camera
ops use. If the probe from the previous interval hasn't come back, the ping is
withheld and the reason logged at `ERROR`. The probe is never cancelled: a pool
that frees up later completes it and pinging resumes on its own; one that doesn't
leaves systemd to abort the process. Worst-case detection is about two intervals
plus the deadline (~45s). This is the software equivalent of a hardware watchdog
timer that you kick from the main loop rather than from a timer ISR — kicking it
from somewhere that keeps running regardless of whether the real work does is the
classic way to build a watchdog that never fires.

## HTTP API

All routes are declared directly on the `FastAPI()` instance in `app/app.py` (no routers
— the surface is small enough that splitting it out would be premature).

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/status` | Returns `{connected, model, recording}` from current `app.state.camera`. Never touches hardware — just reads state (including the `recording` flag). |
| `POST` | `/api/connect` | Forces a connection attempt via `_connect_if_needed` (reuses the same lock as the watcher). 503 if still no camera after trying, or if an attempt was already in flight and this one gave up waiting for it (`CONNECT_TIMEOUT`). |
| `POST` | `/api/capture` | 503 via `_require_camera()` if disconnected. Otherwise runs `cam.capture()` in a threadpool and returns the saved file path. Returns 409 if a recording is in progress (stills and video are mutually exclusive on the body). |
| `POST` | `/api/bulb` | Body `{seconds}` (float, `0 < seconds <= MAX_BULB_SECONDS`, default 900 — override per device with `PATHFINDER_MAX_BULB_SECONDS`; out-of-range, `inf`, or `nan` are rejected with 422 before any hardware is touched, because the value directly controls how long the physical shutter stays open). 503 if disconnected. Opens the bulb shutter, holds `seconds`, closes it (retried — see `camera.md`), then waits `seconds + BULB_READOUT_MARGIN` for the frame and downloads it — returns `{ok, path}`. The readout window scales with the exposure because long-exposure NR shoots a dark frame of comparable length. Returns 409 if a recording is in progress or the body exposes no bulb widget; 400 on any other failure (e.g. no frame produced). The body must be in Bulb exposure mode — the app fires the release, and does not yet set *or check* the mode, so a request in any other mode still returns `{ok}` for a frame it didn't take (TODO #38). |
| `POST` | `/api/afpoint` | Body `{x, y}` (frame-normalized floats in `[0, 1]`, origin top-left — typically from a tap on the live preview). 503 if disconnected. Moves the AF point via `cam.set_af_point()`, returns `{ok}`. 400 if the body exposes no AF-area widget or isn't in a spot focus-area mode. |
| `GET` | `/api/liveview` | 503 if disconnected. Otherwise streams `multipart/x-mixed-replace` — a continuous MJPEG feed, one `cam.preview()` frame per part, that an `<img>` decodes in place. Each frame is a separate `_run_camera(cam.preview)` call, so it grabs the camera lock, pulls one frame, and releases, letting capture/record/settings interleave between frames. The generator stops when the client disconnects (`request.is_disconnected()`) or when the camera drops (a 503 from `_run_camera` breaks the loop and the watcher rebuilds it); it is paced by `LIVEVIEW_FRAME_INTERVAL`. A **409** (the bus is busy with a capture or an exposure) is not a reason to end the stream — the frame is skipped, and the loop retries after `LIVEVIEW_RETRY_INTERVAL`, so the preview comes back on its own when the bus frees up. |
| `POST` | `/api/record/start` | 503 if disconnected. Sets the vendor's movie toggle widget on (`cam.set_recording(True)`), returns `{ok, recording}`. Idempotent — a no-op if already recording. 400 if the body exposes no movie widget. |
| `POST` | `/api/record/stop` | Mirror of the above with the toggle off. Idempotent — a no-op if not recording. |
| `POST` | `/api/autofocus` | 503 if disconnected. Triggers a one-shot autofocus via `cam.autofocus()`, first switching the body into an AF mode if needed. Returns `{ok, focusmode}` (the effective mode, or `null` if the body opts out of mode management). 400 if the body exposes no AF-drive widget. |
| `POST` | `/api/focus` | Body `{steps}` (signed int, magnitude at most `MAX_FOCUS_STEPS` = 10000 — a sanity bound only; the real limit is the focus widget's own advertised range, which `camera/gp2.py` clamps to, logging when it does). Switches the body into a manual-focus mode if needed, then nudges via `cam.manual_focus(steps)`. Returns `{ok, focusmode}`. 400 if the body exposes no manual-focus-drive widget. Both focus routes are allowed while recording (rack focus). |
| `GET` | `/api/magnifier` | 503 if disconnected. Returns `{supported, levels, value}` for the body's focus-magnifier action — `levels` are the widget's own choices (`["Off", "1", "5.5", "11"]` on the α7 IV), `value` the current one. A body with no magnifier widget answers `{"supported": false, "levels": [], "value": null}` rather than erroring, so the client can ask unconditionally and hide the control. |
| `POST` | `/api/magnifier` | Body `{level}` (string — one of the labels `GET` reported). 503 if disconnected. Punches the liveview in or out and returns the re-read `{supported, levels, value}`. **400** if the level isn't one the body offers (checked against the widget's choices *before* anything is sent to USB) or if the body exposes no magnifier widget. Allowed while recording, like the focus routes. The read-back goes through a **whole-tree `get_config()`** — on Sony a single-widget read serves a cache the write doesn't invalidate, so the response would report the *previous* level and the UI would sit one change behind; see `camera.md`. The read-back is retried up to `MAGNIFIER_SETTLE_ATTEMPTS` times, because the body can also be mid-apply; if it still disagrees the response reports what the body says, not what was asked, and the server logs a `WARNING`. Costs ~410ms of held bus settled, ~920ms when a retry fires, so liveview drops a few frames per change (TODO #49). |
| `GET` | `/api/telemetry` | 503 if disconnected. Returns the body's read-only `status` widgets (battery, frames remaining, model, serial, lens) as a list of `{name, label, value}`. Separate from `/api/settings` because these are informational, not editable; a widget the driver lists but can't poll comes back with `value: null` rather than failing the whole request. |
| `GET` | `/api/settings` | Returns the camera's current settings as a list of widget descriptors (see below). Each descriptor carries `readonly`. Rows the body reports read-only are **included when they are a choice with options** — the browser renders those as disabled controls, so a setting the camera owns right now (aperture and shutter speed in `P`) is visibly present and inert rather than silently absent. Read-only *ranges* are excluded: a slider that cannot move is a readout, not a setting, and on the α7 IV all three of them report values their own advertised range forbids or that belong in telemetry. See `camera.md`. |
| `POST` | `/api/settings/{name}` | Body `{value}` (str/int/float/bool). Sets one setting. **404** if `name` is not *writable* — action widgets (shutter, focus drives), status widgets, and any widget the body reports read-only are all unreachable here. Note the read and write surfaces are deliberately **not** the same set: `/api/settings` lists read-only rows that this endpoint refuses, so `readonly` on a descriptor is the browser's cue not to offer an edit, and this 404 is the enforcement. 400 with the underlying error on any other failure. On success re-reads and returns the full settings list so the UI can pick up any settings that changed as a side effect (e.g. aperture limits shifting with ISO). |

`_require_camera()` is the single guard used by every route that needs hardware —
raises `HTTPException(503, "no camera connected")` if `app.state.camera is None`.

`app.mount("/", StaticFiles(directory="web", html=True))` is registered **last**, so
it acts as a catch-all serving `web/index.html` and assets after the `/api/*` routes
have had first refusal.

## `camera/` package — the gphoto2 boundary

`app/app.py` never touches the `gphoto2` binding directly; everything goes through
`camera/`, whose public surface is just five names re-exported from `gp2` (the
only import `app/app.py` makes is `import camera`): `connect()`, `disconnect()`,
`is_disconnect_error()`, and the `CameraDisconnected` / `CameraBusy` exceptions.

`connect()` returns a `Gphoto2Camera` — one instance per physical connection that
holds a `threading.Lock` guarding **every** hardware op (capture, preview, focus,
recording, settings), because `run_in_threadpool` runs them on worker threads that
would otherwise overlap inside libgphoto2. The methods `app/app.py` calls (`capture`,
`preview`, `set_recording`, `autofocus`, `manual_focus`, `magnifier`,
`set_magnifier`, `list_settings`, `set_setting`) and the per-model quirk
resolution in `sony.py` are documented in
depth in **`camera.md`** — this section is only the app-facing boundary, not a
repeat of that.

## Concurrency model, summarized

Two independent forms of serialization protect the single camera object:

1. `asyncio.Lock` (`_connect_lock` in `app/app.py`) — serializes *connection attempts*
   (watcher vs. explicit `/api/connect`).
2. `threading.Lock` (`Gphoto2Camera._lock` in `gp2.py`) — serializes *operations on
   an already-connected camera* (capture vs. settings read/write), since those run
   as separate threadpool calls that could otherwise interleave.

There is no request queue beyond these locks, and **both are acquired with a
deadline** — a concurrent request waits briefly and is then refused (409 for the
camera lock, 503 for the connect lock) rather than waiting indefinitely. So a
`/api/settings` call issued during a short capture still just waits, but one
issued during a 900s bulb — or behind an operation that has wedged the bus —
fails fast instead of consuming a worker for the duration. That distinction is
what keeps a single stuck USB call from spreading through the pool; see "Timeouts
and the watchdog" above.

## Frontend contract (for context)

`web/script.js` polls `/api/status`, and when connected, renders `/api/settings`'s
widget list dynamically (choice/toggle/range/text controls) and posts edits to
`/api/settings/{name}`. It has no camera-specific logic — everything it needs to
render a control comes from the widget descriptor shape defined in `gp2._describe`.
