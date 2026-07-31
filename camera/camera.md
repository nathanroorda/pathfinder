# Camera Layer

The `camera/` package is Pathfinder's **hardware boundary**: the only code that
imports the `gphoto2` binding and touches USB. Everything above it (`app/app.py`, and
by extension `web/`) talks to a camera exclusively through this package's small
public surface, so the rest of the app has no `gphoto2`-shaped types leaking into
it and stays camera-agnostic.

This document covers the package internals — the connection object, the widget
data model, quirks, and disconnect classification. For how the *app* drives this
layer (the connection lifecycle, the background reconnect watcher, and the
`asyncio` vs. `threading` split), see **`app/app.md`**; that context isn't repeated
here.

```
app/app.py ──import camera──▶ camera/__init__.py ──▶ gp2.py ──▶ libgphoto2 (USB)
                                                    │
                                                    └──▶ sony.py (per-model quirks)
```

## Files

- **`__init__.py`** — the public surface. Re-exports exactly five names from
  `gp2`: `connect`, `disconnect`, `is_disconnect_error`, and the
  `CameraDisconnected` / `CameraBusy` exceptions, and pins them in `__all__`.
  `app/app.py`'s only import from this package is `import camera`, so this list *is*
  the contract — anything not re-exported here is a package-internal detail.
  (The exceptions are `CapWords` because they're classes; the rest are
  `snake_case` functions — the standard Python split, not an inconsistency.)
- **`gp2.py`** — the libgphoto2 backend. Connection, capture, recording,
  settings read/write, telemetry, and the disconnect-error classification all
  live here.
- **`sony.py`** — a per-model **quirk table**. No `gphoto2` calls; pure data plus
  a lookup function. This is the one file you add to when onboarding a new
  camera body.

## `connect()` / `disconnect()`

`connect()` calls `gp.Camera().init()` (the blocking USB handshake), reads the
model string off `get_abilities()` — falling back to a generic
`"USB camera (gphoto2)"` label if the body doesn't report one — and wraps the
handle in a `Gphoto2Camera`. `disconnect(camera)` just delegates to the object's
`close()`. Both are synchronous and blocking; `app/app.py` is responsible for keeping
them off the event loop (via `run_in_threadpool`).

## `Gphoto2Camera` — one instance per physical connection

The object owns the live `gphoto2` handle (`_cam`) and all state tied to it. Two
design points drive everything else in the class:

**1. One lock guards every hardware op, and waiting on it is bounded.** `_lock`
(a `threading.Lock`) wraps the body of `capture`, `bulb`, `preview`,
`set_recording`, `autofocus`, `manual_focus`, `set_af_point`, `list_settings`,
`set_setting`, `telemetry`, and `close`.
This matters because `app/app.py` runs these on threadpool workers — without the
lock, a capture and a settings write could execute inside libgphoto2
concurrently, which the binding doesn't tolerate. `_require_open()` (called at the
top of each locked block) raises `CameraDisconnected` if `close()` has already
nulled `_cam`; because `close()` takes the *same* lock, once `_require_open()`
passes, `_cam` is guaranteed valid for the rest of that block.

Every one of those blocks goes through the `_bus(operation)` context manager
rather than `with self._lock:` directly, and `_bus` **acquires with a deadline** —
`BUS_TIMEOUT` (2s), or the shorter `PREVIEW_BUS_TIMEOUT` (0.25s) for liveview
frames — raising `CameraBusy` if it expires. The reason is that everything under
this lock is a blocking C call into USB that nothing can interrupt: a body that
wedges mid-PTP-transaction keeps its caller's threadpool worker permanently, and
an unbounded `with self._lock:` would then consume one more worker per queued
request until the pool (40) is gone and the process is inert while still very much
"running" (TODO #8). Bounding the *wait* can't rescue the stuck operation — no
Python-level timeout can interrupt a C call — but it keeps one wedge from taking
the server with it. `app/app.py` maps `CameraBusy` to **409**; it is deliberately
*not* a disconnect error, so a busy bus never tears down a healthy connection.
The refusal names the current holder (`camera is busy with bulb`), which on a
headless device is most of the diagnosis. Preview gets the shorter deadline
because a liveview frame queued behind a capture is stale by the time it lands —
better to skip it and take a fresh one.

The same reasoning applies to `close()`, which can therefore fail: if it can't
take the bus it logs at `ERROR` that the USB claim stays open and re-raises, so
the caller (`_drop_camera`, shutdown — both of which suppress it) doesn't block
on a wedged handle either. The claim is released when the holding operation
finally returns and the handle is collected.

**2. `close()` is idempotent and race-safe.** It swaps `_cam` to `None` under the
lock before calling `exit()`, so a second `close()` (or a `close()` racing an
in-flight op) is a clean no-op rather than a double-free of the USB handle.

### The `recording` flag — a deliberate lock exception

`self.recording` is *written* under `_lock` inside `set_recording`, but *read*
lock-free by `app/app.py`'s `/api/status` route. That's intentional and safe: a lone
`bool` read/write is atomic under the GIL, so status polling never needs to wait
on an in-flight capture just to learn the recording state. Only the
check-then-act inside `set_recording` (compare requested state to current, no-op
if equal) needs the lock, because that's a compound operation. This is the same
reasoning you'd apply to an ISR-shared flag on an MCU: a single-word load/store is
atomic, but read-modify-write is not, so only the latter needs a critical
section.

### `capture()`

Runs entirely under `_lock`, in four steps:

1. **Enforce `shot_gap`** — sleep out the remainder of the per-model minimum
   interval since `_last_shot`, so rapid taps don't outrun what the body can
   handle.
2. **`_drain_events()`** — flush any queued camera events first
   (`wait_for_event` in `DRAIN_POLL_MS` slices until `GP_EVENT_TIMEOUT`); stale
   events left in the queue can otherwise interfere with the capture call on
   some bodies. **Bounded by a `DRAIN_TIMEOUT` (1s) deadline**, because the
   *body* decides when the queue runs dry: a Sony streaming property-change
   events — a dial being turned, some live-view states — never sends the timeout
   event, and this loop runs with `_lock` held, so an unbounded wait parks every
   other request behind it (TODO #7). On expiry it logs at `WARNING` and
   proceeds with events still queued; finishing the capture matters more than a
   perfectly empty queue, and a hang here is indistinguishable from a bricked
   device. Same reasoning as never polling a status register without a timeout —
   the peripheral is allowed to misbehave, your loop is not.
   A `GPhoto2Error` while draining is logged (at `WARNING` if
   `is_disconnect_error()` claims it, `DEBUG` otherwise) but **not** re-raised:
   the drain is hygiene the caller didn't ask for, and the capture that follows
   hits the same dead bus and raises there, where it can be attributed to a
   request. Raising here would also abort the retry in step 3 on the strength of
   a failed *pre*-op read.
3. **`_capture_with_retry()`** — call `capture(GP_CAPTURE_IMAGE)`, retrying up to
   `capture_retry_attempts` times on a generic `GP_ERROR`, with a 1s backoff and
   another event drain between tries. Transport-level errors are *not* retried
   here — they propagate so the app can drop and rebuild the connection (see
   below).
4. **Download** the resulting file to `CAPTURE_DIR` (env `PATHFINDER_CAPTURE_DIR`,
   default `captures/`), prefixed with a unix timestamp to avoid name
   collisions. Returns the saved path.

A capture is refused with `RuntimeError` if `self.recording` is set — stills and
video are mutually exclusive on the body — which `app/app.py` surfaces as a 409.

The download half (make dir → timestamp-prefix the name → `file_get().save()`) is
factored into `_download(path, save_dir)`, shared with `bulb()`.

### `bulb(seconds)`

A **timed manual exposure**, for shots longer than the body's fastest bulb-less
shutter. It writes the vendor's bulb-release action (`bulb_widget`, `"bulb"` on
Sony) high to open the shutter, sleeps `seconds`, then writes it low to close —
a rising then falling **edge** on the release line, the software analog of
pressing and letting go of a cable release.

The falling edge goes through `_release_action` rather than `_drive_action`,
because it is the write that strands hardware if it never lands. It retries
`RELEASE_ATTEMPTS` times with a `RELEASE_RETRY_DELAY` backoff, logs at `ERROR`
naming the latched widget if every attempt fails, then re-raises — so a transport
failure propagates as a disconnect, the app drops the camera, and closing the
handle at least releases the USB claim. If the bus is genuinely gone no retry can
close the shutter; that residual is the reason the ceiling in #1 exists. A
failure *during* the exposure (`except BaseException`, so `KeyboardInterrupt`
counts) still attempts the release but keeps its own exception rather than being
masked by the release's.

The whole exposure runs under `_lock`, i.e. the sleep **holds the camera lock for
its full duration** — deliberately: no other PTP traffic (preview, telemetry,
another capture) may share the bus during an open exposure, and the lock is what
enforces that. The frontend tears its preview stream down for the same reason.
Everything else that arrives during those seconds is refused with `CameraBusy`
(→ 409, naming `bulb`) after `BUS_TIMEOUT` rather than queueing; a disconnect
still can't be reaped until the exposure ends, which is the cost any long
exposure carries.

Unlike `capture()`, the body doesn't hand back a path synchronously: after the
close it writes the frame asynchronously and announces it with a
`GP_EVENT_FILE_ADDED` event. `_wait_for_image(timeout)` polls `wait_for_event` in
500 ms slices for that event's `CameraFilePath`, then `_download`s it exactly as
`capture()` does. The deadline is `seconds + BULB_READOUT_MARGIN`, **not** a fixed
cap: with Long Exposure NR on, the body shoots a dark frame of roughly equal
length before the file appears, so a fixed window fails every long exposure while
the frame lands on the card anyway. The margin covers the write itself and is a
guess until measured on a real body. Refused with `RuntimeError` (→ 409) while
recording, or if `bulb_widget` is `None` (body opts out).

> **Body requirement:** `bulb` only opens the shutter when the body's exposure
> mode is actually **Bulb** (shutter-speed dial / `shutterspeed` = `BULB`). The app
> fires the release; it does not force the mode, and — confirmed on an α7 IV
> 2026-07-25 — does not *detect* the mode either: with the dial in `P`, driving
> the bulb widget acts as a plain shutter release, so a 30 s request produced a
> 1 s Program-AE frame and still returned `{"ok": true}`. Reporting success for
> work not performed is tracked as **TODO #38**; the fix is to read the exposure
> mode before driving and refuse (→ 409) if it isn't Bulb.

### `set_recording(on)`

Starts/stops movie recording by writing the vendor's **movie toggle widget**
(`_quirks["movie_widget"]`, default `"movie"`) to `1`/`0` via `set_config()`.
Idempotent: compares against the tracked `recording` flag and returns early if
already in the requested state. The movie widget lives in gphoto2's `actions`
config section, which `INCLUDE_SECTIONS` deliberately excludes, so it never shows
up as a settings row in the UI — recording is a button, not a setting.

### `autofocus()` / `manual_focus(steps)`

Two focus commands that both funnel through the private `_drive_action(name,
value)` helper. `autofocus()` writes the vendor's **AF-drive widget**
(`_quirks["af_widget"]` — `"autofocus"` on Sony, `"autofocusdrive"` for an
unknown body) to trigger a one-shot autofocus; `manual_focus(steps)` writes the
**manual-focus-drive widget** (`_quirks["manual_focus_widget"]` — `"manualfocus"`
on Sony, `"manualfocusdrive"` otherwise) with a signed step count — negative drives focus nearer, positive farther, and magnitude sets
how far. On the α7 IV `manualfocus` is a `RANGE` of `-7..7` (idle `0`); the
frontend maps its Fine/Med/Coarse selector to `1`/`3`/`6` to spread across that
travel. The direction sign and step magnitudes are body-specific; if a new body
focuses the wrong way, swap the sign at the call site or adjust the quirk — no
change here.

`autofocus()` writes the sequence in the `af_drive_values` quirk to `af_widget`,
one fresh-config edge per value. It unpacks the sequence as `*press, release`: the
leading values go through `_drive_action`, and the **last** one — the value that
returns the widget to rest — goes through `_release_action`, the same
retry-and-shout helper `bulb()` uses, and is attempted even if a press raises.
A latched AF toggle keeps the lens hunting and blocks the next capture, so it gets
the same fail-safe treatment as the shutter. One consequence worth knowing: on a
body whose sequence is a single `(1,)` there is no separate release, so that lone
trigger is what gets retried. The generic default is a
single `(1,)`; Sony overrides it with `(1, 0)` — a **press/release**, because the
α7 IV `autofocus` toggle idles at `2` and a lone `1` would leave the shutter in an
AF-lock half-press. Press (1) runs AF, release (0) completes the one-shot, and
under AF-S the body keeps focus locked at the distance it found. Keeping the
sequence in data (not hardcoded here) is what stops Sony's protocol from being
imposed on a generic body, where an unconditional `0` could cancel the AF the `1`
just started.

**Focus-mode gating — `_ensure_focus_mode(acceptable, target)`.** A focus command
is accepted over PTP but silently ignored if the body is in the wrong mode:
`manualfocus` only drives the motor in `Manual` (and possibly `DMF`), and
`autofocus` only fires outside `Manual`. So each action first ensures the mode.
This is a **guarded read-modify-write against the body's `focusmode` register**,
run inside the caller's already-held `_lock` so the read/decide/switch is atomic
with respect to every other camera op (the same reason you'd wrap a non-atomic
RMW on an ISR-shared register in a critical section). `acceptable` is a *set* of
modes, not one value: if the live mode is already in it, the helper returns
without writing — idempotent, and it leaves the user's own `AF-C`/`DMF` choice
alone. Only when the mode is unacceptable does it switch to `target`. It returns
the effective mode (which `autofocus`/`manual_focus` return on up to `app/app.py`, so
the API can report it and the UI can refresh the now-stale `focusmode` row), or
`None` when the body opts out of mode management (`focus_mode_widget` is `None`).

The mode is **latched, not restored**: the buttons express intent, so the body
stays in the mode the last focus action needed rather than being reverted.

Both widgets live in gphoto2's `actions` section (excluded from
`INCLUDE_SECTIONS`), so like `movie` they're driven directly, never shown as
settings rows — focus is a button, not a stored setting.

`_drive_action` uses the **single-config** path — `get_single_config(name)` →
`set_value` (coerced by `_coerce` to the widget's gphoto2 type) →
`set_single_config(name, widget)`. This is an **efficiency choice, not a
requirement**: single-config reads and writes just the one property, whereas the
whole-tree `get_config()`/`set_config()` that `set_setting` uses round-trips the
entire config over PTP — wasteful for a focus nudge that may fire rapidly. The
full tree works on these action widgets too (verified), so this is purely about
cost. Re-reading the widget per call also means each write starts from its live
value, so it's a real edge that re-fires the momentary action rather than a no-op
— the software analog of edge-triggering a shutter line.

> **History (so the `-2` ghost doesn't come back):** a long bug hunt blamed the
> `single-config`↔`full-tree` distinction — the theory was that action widgets
> *needed* single-config and `focusmode` *needed* the tree. That was wrong. The
> `-2 Bad parameters` was always a **wrong widget name**: the α7 IV's model string
> didn't match `MODELS`, so quirks fell back to `DEFAULT_QUIRKS`, whose generic
> `"autofocusdrive"` doesn't exist on the body (see the `sony.py` section). Both
> config APIs work on both widget kinds; the mechanism was never the problem.

Unlike `capture`/`preview`, these are **not** gated on `self.recording`: they're
plain config writes (as `set_setting` is), and driving focus mid-recording is a
deliberate use case (rack focus during video).

### `set_af_point(x, y)`

Moves the AF point to where the user tapped the live preview. To keep the frontend
camera-agnostic, `x`/`y` arrive **frame-normalized** — floats in `[0, 1]`, origin
top-left — i.e. "a fraction of the way across the frame," carrying no knowledge of
the body's coordinate grid. `_scale(fraction, size)` clamps to `[0, 1]` (a stray
tap outside the image can't push the point off-sensor) and maps onto the body's
native grid from the `af_area_size` quirk, and the two integers are handed to the
`af_area_widget` (`"spotfocusarea"` on Sony) as a `"x,y"` string — a `TEXT` widget,
so `_coerce` passes it through untouched. Refused with `RuntimeError` (→ 400) if
`af_area_widget` is `None`. Like the focus actions it's driven directly through
`_drive_action`, not shown as a settings row.

> **The widget name was wrong until 2026-07-30.** This quirk read `"changeafarea"`
> — the Canon EOS name — and `/main/actions/changeafarea` does not exist anywhere
> in the α7 IV tree, so `/api/afpoint` could only ever have raised
> `[-2] Bad parameters`. The hardware dump in `tests/fixtures/ilce_7m4.txt`
> settles it: the widget is `/main/actions/spotfocusarea`, `TEXT`, writable.
> The tests missed it because the fake published `changeafarea` too — it was
> built from this quirk table rather than from a body. See
> `tests/fixtures/fixtures.md`.

> **Still to verify on hardware.** (1) `af_area_size` — the native grid the
> fraction scales onto, still defaulted to Canon's `(640, 480)`. The dump can't
> settle this one: `spotfocusarea` is a `TEXT` widget reporting an empty current
> value, so it advertises no range. Corner taps are the way — set `(1, 1)`, tap
> each corner, and read back what the body accepted. (2) The widget requires a
> **spot / flexible focus-area mode** — in a Wide mode the point can't move and
> the body ignores or rejects the write. The `focusarea` widget on this body does
> offer `Flexible Spot: S/M/L`, so managing that mode automatically (à la
> `_ensure_focus_mode`) is a natural follow-up if it proves fiddly in practice.

### `preview()`

Pulls a single **liveview frame** by calling `capture_preview()` and returning
the JPEG bytes (`get_data_and_size()`). Like every other op it runs under `_lock`
and grabs *one* frame per call — the caller (`app/app.py`'s `/api/liveview` MJPEG
loop) reacquires the lock for each successive frame, so a capture, record, or
settings write can interleave between frames instead of being starved by a
long-held stream. It is the one op with its own, shorter acquisition deadline
(`PREVIEW_BUS_TIMEOUT`), for the reason given above. It refuses with `RuntimeError` while `self.recording` is set:
issuing extra PTP `capture_preview` traffic on the bus while a movie is rolling
risks disturbing the recording, so previews and recording are kept mutually
exclusive (the frontend also tears its stream down when recording starts, so
this guard rarely fires — it's the backstop for a direct API hit).

### Settings: `list_settings()` / `set_setting()` and the widget model

This is what makes the UI camera-agnostic. Rather than hardcoding controls,
Pathfinder reflects whatever the connected body exposes:

- **`_settable_widgets(config)`** is the single definition of "a setting": it
  walks the camera's config tree from `get_config()`, recursing through
  `WINDOW`/`SECTION` container nodes (`_walk`) and yielding only leaf widgets
  that are (a) under one of `INCLUDE_SECTIONS`
  = `{imgsettings, capturesettings, settings}`, (b) of a type Pathfinder knows
  how to render, and (c) not read-only.
- **`list_settings()`** turns each of those into a plain descriptor dict by
  `_describe`.
- **`set_setting(name, value)`** resolves the name through `_settable_widget`
  against that *same* generator, coerces `value` to the type gphoto2 expects for
  that widget kind (`_coerce`), and writes it back with `set_config()`. A name
  outside the allowlist raises `KeyError`, which `app/app.py` maps to **404**.
  Having one definition shared by both paths is the point: they previously
  drifted, and `set_setting` resolved names against the *whole* tree — so
  `POST /api/settings/bulb` reached the shutter release through an endpoint that
  is supposed to be inert (TODO #6, fixed and verified 2026-07-25). On the α7 IV
  this scoping takes the writable surface from every widget in the tree down to
  the 20 `choice` widgets the listing offers.
- **`telemetry()`** is the read-only counterpart: it walks the same config tree
  but keeps leaf widgets under `STATUS_SECTIONS` = `{status}` — the battery,
  frames-remaining, model, serial, and lens fields the body reports but you don't
  edit. The two surfaces cannot overlap, and the reason is the **section split**,
  not the read-only filter: `status` is not in `INCLUDE_SECTIONS`, so a status
  widget never reaches `_settable_widgets`' filters at all. Worth being precise
  about, because the guarantee is stronger than "they happen to be read-only" —
  a body that advertised a *writable* widget in its `status` section would still
  be kept out of the settings panel. Each is reduced to a
  bare `{name, label, value}` by `_describe_status` (no `type`/`choices`/`range`,
  since nothing renders them as editable controls). Reading an individual status
  widget's value can fail on some bodies — a prop the driver advertises but can't
  poll — so `_describe_status` swallows that `GPhoto2Error` and reports `value:
  None` rather than letting one bad widget sink the whole panel.

The type mapping (`_KIND`) collapses gphoto2's widget types into four render
kinds — this is the vocabulary the frontend renders against:

| gphoto2 widget type | descriptor `type` | coercion (`_coerce`) | extra descriptor fields |
|---|---|---|---|
| `RADIO`, `MENU` | `choice` | `str` | `choices: [...]` |
| `TOGGLE` | `toggle` | `int` | — |
| `RANGE` | `range` | `float`, **clamped to `get_range()`** | `min`, `max`, `step` |
| `TEXT` | `text` | `str` | — |

`_coerce` takes the **widget**, not just its type, precisely so the `RANGE` case
can read the bounds the body advertises and hold the value inside them — the only
bounds that know where the hardware stops. Nothing above this layer can: an
out-of-range `/api/focus` step used to go straight to the lens motor (TODO #5).
A clamp logs at `WARNING` naming the widget and both values, since silently
correcting a caller hides a client bug. `NaN` is **rejected** with `ValueError`
(→ 400) rather than clamped: `max(low, nan)` returns `low`, so an unchecked clamp
would have driven the widget to one end of its travel. Step *granularity* is not
enforced — an off-grid value on a step-1 widget is still sent as-is.

Every descriptor carries `name`, `label`, `type`, and `value`. This shape is the
**contract with the frontend** — `web/script.js` renders a control purely from
these fields and knows nothing about gphoto2. It's documented from the consumer
side in **`web.md`**; keep the two in sync if you extend `_describe`.

## Disconnect classification — `is_disconnect_error()` and `_DISCONNECT_CODES`

The single most important distinction this layer draws is **transport failure vs.
logical error**, because the two demand opposite responses:

- A *transport* failure (`GP_ERROR_IO` -7, `GP_ERROR_IO_USB_FIND` -52,
  `GP_ERROR_IO_USB_CLAIM` -53, and the other I/O codes in `_DISCONNECT_CODES`)
  means the USB handle is dead — the cached handle no longer resolves to a device
  on the bus. It is **unrecoverable on the existing handle**; the only fix is to
  drop the `Camera` and `init()` a fresh one. `GP_ERROR_IO_USB_FIND` (-52) is the
  specific code a Sony body throws after re-enumerating on the bus mid-capture.
- A *logical* error (a bad setting value, a capture refused mid-recording) means
  the request was wrong but the connection is fine — it should surface to the
  caller and leave the handle intact.

`is_disconnect_error(exc)` is the predicate that makes this call: `True` for a
`CameraDisconnected` (raised locally when an op finds `_cam` already closed) or a
`gp.GPhoto2Error` whose `.code` is in `_DISCONNECT_CODES`. `.code` is only read
after the `isinstance(exc, gp.GPhoto2Error)` guard, since that attribute only
exists on gphoto2 errors. `_DISCONNECT_CODES` is built with `getattr`/`hasattr`
so it degrades gracefully across libgphoto2 versions that may not define every
constant.

`app/app.py` uses this predicate (in `_run_camera`) to decide between dropping the
connection for a background rebuild (503, self-healing) and passing the error
through (400/409). That recovery flow — and why a dead handle would otherwise fail
identically forever — is documented in **`app/app.md`**.

## `sony.py` — per-model quirks

A small data module with no hardware calls. `quirks(model)` returns `None` for a
**non-Sony** body (`"sony"` not in the model string), and otherwise a dict built
by layering any matching `MODELS` override on top of the `GENERAL` Sony defaults.

**Match on what gphoto2 actually reports, by substring.** `connect()` feeds
`quirks()` the `get_abilities().model` string — for the α7 IV that's
`"Sony Alpha-A7 IV (PC Control)"`, **not** the USB/internal `"ILCE-7M4"` name.
Two rules follow, and both are why a `-2` "bad parameters" haunted the focus
buttons before this was fixed:

- **Sony detection is `"sony" in model.lower()`**, so *any* Sony body inherits
  `GENERAL` (correct focus widget names, etc.) even if it has no `MODELS` entry.
  The old exact-match-on-`"ILCE-7M4"` fell through to `DEFAULT_QUIRKS`, whose
  generic-PTP `"autofocusdrive"`/`"manualfocusdrive"` names **don't exist on the
  body** — so every focus write hit `get_single_config("autofocusdrive")` → `-2`.
- **`MODELS` keys are matched as substrings** (`key.lower() in model.lower()`),
  which tolerates the varying suffixes gphoto2 appends (`(Control)` vs
  `(PC Control)`, firmware revisions). Hence the α7 IV key is `"A7 IV"`, a token
  that appears in the reported string — not the internal name that never does.

The quirk keys:

| key | meaning | `GENERAL` (Sony) | `DEFAULT_QUIRKS` (unknown body) |
|---|---|---|---|
| `shot_gap` | min seconds between stills | `1.5` | `0.0` |
| `capture_retry_attempts` | tries on a generic `GP_ERROR` | `2` | `1` |
| `movie_widget` | config name of the movie toggle | `"movie"` | `"movie"` |
| `af_widget` | config name of the AF-drive action | `"autofocus"` | `"autofocusdrive"` |
| `af_drive_values` | value sequence written to `af_widget` per trigger | `(1, 0)` (press/release) | `(1,)` |
| `manual_focus_widget` | config name of the manual-focus-drive action | `"manualfocus"` | `"manualfocusdrive"` |
| `focus_mode_widget` | config name of the focus-mode selector (`None` = don't manage mode) | `"focusmode"` | `None` |
| `af_modes` | modes in which `autofocus` fires (no switch if already one) | `("Automatic", "AF-A", "AF-C", "AF-S", "DMF")` | `()` |
| `af_target_mode` | mode to switch to for AF when outside `af_modes` | `"AF-A"` | `None` |
| `mf_modes` | modes in which `manualfocus` drives the motor | `("Manual",)` | `()` |
| `mf_target_mode` | mode to switch to for manual focus when outside `mf_modes` | `"Manual"` | `None` |
| `bulb_widget` | config name of the bulb-release action (`None` = unsupported) | `"bulb"` | `None` |
| `af_area_widget` | config name of the AF-area/point action (`None` = unsupported) | `"spotfocusarea"` | `None` |
| `af_area_size` | native AF-grid size `(w, h)` the normalized tap is scaled onto | `(640, 480)` | `(0, 0)` |

`gp2._quirks_for(model)` walks each module in `VENDORS = [sony]` in order, taking
the first non-`None` result, and falls back to `DEFAULT_QUIRKS` for anything
unrecognized. So an untuned camera still works — just with conservative timing and
a single capture attempt. It **logs which path it took** — `INFO "matched vendor
quirks for model …"` on a hit, `WARNING "no vendor quirks matched …"` on the
fallback — because a *silent* fallback to generic widget names is exactly what
made the focus `-2` so hard to find. If focus misbehaves on a new body, that
warning line is the first thing to check.

**Every widget name in that table is checked against real hardware.** The dumps
in `tests/fixtures/` are captured off actual bodies, and
`test_fake_fidelity.EveryDumpedBodyMatchesItsQuirks` asserts that each name a
quirk table uses exists in the tree of every body the table claims. Nothing else
catches a wrong name before it reaches USB — `get_single_config` raises `[-2] Bad
parameters` at runtime and no earlier layer looks. That check is why
`af_area_widget` is now known-good rather than assumed; see `tests/tests.md` for
why the *target* modes are asserted strictly while `af_modes` / `mf_modes` are
only required to overlap.

**Adding a vendor:** create a module exposing a `quirks(model)` function with the
same contract and append it to `VENDORS`. **Adding a model to an existing
vendor:** add an entry to that vendor's `MODELS` keyed by a distinctive substring
of the *reported* model string (check it with `GET /api/status` or
`get_abilities().model` — don't assume the internal name); an empty dict means
"use the vendor defaults," which is what the α7 IV (`"A7 IV"`) currently does. No
changes to `gp2.py` are needed for either. Capture a dump for the new body with
`tools/camera-dump.sh` at the same time — see `tests/fixtures/fixtures.md`.

## Logging

All modules here log through a per-module `logging.getLogger(__name__)` and never
configure handlers themselves — records propagate to the root logger set up by
`logs/log.py`. See **`logs/log.md`** for the logging architecture; the `DEBUG` level (the
current default) is what surfaces the capture-retry and reconnect breadcrumbs.
