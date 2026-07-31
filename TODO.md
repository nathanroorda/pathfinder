# Pathfinder — Outstanding Issues

Findings from a full-codebase review (2026-07-25), plus a second pass on
2026-07-30 after the watchdog work landed (#39-#46, in "Later findings" at the
end). Ordered by suggested fix order: physical-hardware risk first, then
correctness, then structure, then hardening.

Each item states **what** is wrong, **why** it matters, and a suggested fix.
Line numbers in #1-#38 refer to the state of the tree at commit `da23621` and
are now several commits stale — `BulbExposure` has moved from `app/app.py:28` to
`:50`, the `StaticFiles` mount from `:284` to `:392`. #39 onward are anchored to
`d841aa4`.

**Legend:** 🔴 critical · 🟠 high · 🟡 medium · ⚪ low / nit
· ✅ fixed & verified · 🧪 fix applied, awaiting hardware verification

---

## Tier 1 — Fix first (small, localized, physical-hardware risk)

### 1. ✅ FIXED — Bulb exposure length is unvalidated — can lock the device with the shutter open

- **Status:** Fixed and **verified on hardware** 2026-07-25.
  `BulbExposure.seconds` is now `Field(gt=0, le=MAX_BULB_SECONDS,
  allow_inf_nan=False)`, bound configurable via `PATHFINDER_MAX_BULB_SECONDS`
  (default 900s). Verified on the Pi: `0`, `-1`, `1e9`, `inf`, `nan`, `900.1` all
  rejected; `1` and `900` accepted (`le` boundary inclusive as intended). Live
  service returns 422 for `1e9`. The client-side `api()` helper now flattens
  FastAPI's array-shaped 422 `detail` — response confirmed to be
  `{"detail":[{"type","loc","msg",...}]}`. Worst case is now a bounded 900s lock,
  not unbounded — the lock-holding itself remains #8/#10.
- **Where:** `app/app.py:28-29` (`BulbExposure`), `camera/gp2.py:101-121`
- **Issue:** `BulbExposure.seconds` is a bare `float`. The browser checks `> 0`
  (`web/script.js:165`); the server checks nothing. `POST /api/bulb {"seconds": 1e9}`
  is accepted. Because `bulb()` holds `self._lock` across `time.sleep(seconds)`,
  that single request opens the physical shutter, never closes it, and holds the
  camera mutex for ~31 years. Negative values reach `time.sleep(-1)` and raise
  `ValueError`.
- **Why it matters:** Any client on the AP — including a mistyped value in the UI —
  can render the device permanently unresponsive *and* leave the shutter open,
  draining the battery and ruining the sensor's thermal state. Client-side
  validation is a UX affordance, never a security or safety boundary; the server
  owns every constraint that protects hardware.
- **Fix:**
  ```python
  from pydantic import Field
  MAX_BULB_SECONDS = 900

  class BulbExposure(BaseModel):
      seconds: float = Field(gt=0, le=MAX_BULB_SECONDS)
  ```

### 2. 🧪 Bulb shutter release is single-shot — a failed write leaves the shutter open

- **Status:** Fix applied 2026-07-25 — **not yet verified on hardware.**
  `_release_action()` retries the release `RELEASE_ATTEMPTS` (3) times with a
  `RELEASE_RETRY_DELAY` (0.2s) backoff, logs `ERROR … may still be latched` if
  every attempt fails, then re-raises the transport error so `_run_camera`
  drops the camera and the watcher rebuilds it. `bulb()` now uses
  `except BaseException` + `contextlib.suppress` rather than `finally`, so a
  failure *during* the exposure keeps its own exception instead of being masked
  by the release's. Unit-covered by `tests/test_gp2_camera.py::Bulb` — transient
  failure retried and shutter ends closed; permanent failure raises and logs;
  interrupted exposure keeps its own error.
- **Residual risk (unfixable in software):** if the bus is gone, no retry closes
  the shutter. The reconnect path releasing the USB claim is the only remaining
  recovery. The original acceptance test asserted the shutter closes even when
  *every* write fails — that was unachievable and has been replaced by the two
  properties above.
- **To verify:** needs the dial in M + BULB. Start a 30s exposure, pull the USB
  cable ~5s in, and confirm the log shows `release bulb=0 failed (attempt 1/3)`
  → `could not release bulb after 3 attempts` → `camera connection lost …
  dropping`, that the client gets 503, and that replugging reconnects within
  ~3s. `uhubctl` may be able to cut port power remotely if the Pi supports it.
- **Where:** `camera/gp2.py:114-117`
- **Issue:**
  ```python
  self._drive_action(widget, 1)
  try:
      time.sleep(seconds)
  finally:
      self._drive_action(widget, 0)   # one attempt, no retry
  ```
  If the release write raises — precisely what happens on the USB glitch this
  codebase already self-heals from elsewhere — the shutter stays open, and the new
  exception masks the original.
- **Why it matters:** This is a fail-safe problem, structurally identical to
  guaranteeing a PWM output is driven low on fault. The "off" path must be the most
  robust path in the function, not the least. Consequence of getting it wrong is
  physical: shutter open until the battery dies.
- **Fix:** Retry the release a bounded number of times with a short backoff, log
  loudly if every attempt fails, and re-raise the original exception rather than
  the release's.

### 3. 🧪 Bulb readout timeout is fixed at 15s — long exposures always report failure

- **Status:** Fix applied 2026-07-25 — **not yet verified on hardware.**
  `_wait_for_image(timeout)` now takes seconds, and `bulb()` passes
  `seconds + BULB_READOUT_MARGIN` (15s). The error message names its own
  deadline (`produced no image within 45s`) so a future failure is diagnosable.
  Unit-covered by `tests/test_gp2_camera.py::Bulb` — the window scales with the
  exposure, and still terminates rather than spinning on the bus.
- **Verification attempt 2026-07-25 was VOID.** The 30s test ran with the mode
  dial in **P**, so the body fired a 1s Program-AE frame and the app simply
  slept. EXIF on the resulting file: `ExposureTime 1/1`,
  `ExposureProgram Program AE` — identical in kind to the ordinary capture taken
  a minute later. Total wall time 32.0s = 30s sleep + ~2s, so readout was never
  stressed and the old 15s deadline would have passed too. Blocked on physical
  access to the mode dial. This void run is what surfaced #38.
- **To verify:** dial to **M**, shutter speed past `30"` to **BULB**, Long
  Exposure NR **On**, then `time curl -s -X POST localhost:8080/api/bulb -d
  '{"seconds": 30}'`. Pass is `ok:true` with wall time **> 45s** (expect ~60s);
  anything under 45s means the dark frame did not happen and the fix is still
  untested.
- **Still open:** the margin is a guess — the a7 IV's actual NR overhead has
  never been measured. Once it is, consider moving it into the quirk table
  rather than a module constant, since it is per-body timing.
- **Still open:** the second half of this item is unaddressed. The timeout now
  rarely fires, but when it does the orphaned `FILE_ADDED` still leaks into the
  next capture's `_drain_events()` and desynchronises the event stream.
- **Where:** `camera/gp2.py:130` (`_wait_for_image(self, timeout_ms=15000)`),
  called at `gp2.py:118`
- **Issue:** The 15-second window starts *after* the exposure ends. With Long
  Exposure NR enabled — the Sony default, and effectively mandatory for the long
  exposures bulb exists to serve — the a7 IV shoots a dark frame of **equal
  length** before the file appears. A 60s bulb needs ~60s more before
  `GP_EVENT_FILE_ADDED` arrives.
- **Why it matters:** The feature is broken for its actual use case. Worse, the
  orphaned `FILE_ADDED` event stays queued and is later consumed by the *next*
  capture's `_drain_events()`, desynchronising the event stream — so one timeout
  can corrupt a subsequent unrelated capture.
- **Fix:** Scale the timeout with the exposure:
  ```python
  path = self._wait_for_image(timeout_ms=int((seconds * 2 + 20) * 1000))
  ```

### 4. 🧪 AF drive has no fail-safe release (same shape as #2)

- **Status:** Fix applied 2026-07-25 — **success path verified on hardware,
  failure path not.** `autofocus()` unpacks `*press, release =
  af_drive_values` and drives the final value through `_release_action` (see
  #2), attempting the release even if a press raises. Unit-covered by
  `tests/test_gp2_camera.py::Autofocus`. Verified on the a7 IV 2026-07-25:
  `POST /api/autofocus` → `{"ok":true,"focusmode":"AF-A"}`, lens focused and
  settled, the capture immediately after succeeded, no release warnings logged.
  Inducing a *failed* release needs the same USB-pull setup as #2.
- **Behaviour change:** bodies with a single-edge `af_drive_values` (the generic
  default `(1,)`) now retry a failed AF trigger, where previously one failed
  write was final. The a7 IV's `(1, 0)` path emits an identical write sequence
  to before.
- **Observed 2026-07-25, unexplained:** the first capture after an AF returned
  `[-1] Unspecified error` and succeeded on `_capture_with_retry`'s second
  attempt. Pre-existing (this is why `capture_retry_attempts: 2` exists) and not
  caused by this change — the success path emits the same writes as before. But
  if the AF→capture correlation repeats, the real cause is likely the body still
  being busy when the capture transaction arrives, and the honest fix is
  draining events after the AF drive rather than letting the retry absorb it.
- **Where:** `camera/sony.py:6` (`af_drive_values: (1, 0)`),
  `camera/gp2.py:179-187` (`autofocus`)
- **Issue:** The loop writes each value in sequence with no `try`/`finally`. If the
  `1` (press) lands and the `0` (release) raises, AF is left half-pressed.
- **Why it matters:** A half-pressed AF holds focus/metering and can block other
  camera operations until the state is cleared. Same fail-safe reasoning as #2 —
  any edge-triggered action needs a guaranteed trailing edge.
- **Fix:** Wrap the drive sequence so the release value is always attempted, and
  treat the "return to idle" write as best-effort-with-retry.
- **See also:** #17 — the a7 IV's `autofocus` toggle idles at `2`, not `0`.

### 5. ✅ FIXED — No server-side clamping of RANGE widgets — `/api/focus` can drive the lens past its stops

- **Status:** Fixed and **verified on hardware** 2026-07-25. `_coerce` now takes
  the *widget* rather than just its type, and a RANGE value is held inside the
  widget's own `get_range()` bounds, logging `clamped <widget> from … to …`
  whenever it bites. `FocusStep.steps` gained
  `Field(ge=-MAX_FOCUS_STEPS, le=MAX_FOCUS_STEPS)` (10000) as the
  defence-in-depth bound — deliberately generous, since the real limit is
  per-body and lives in the widget.
- **Verified on the a7 IV** via `/api/focus`: `3` → 200 with **no** clamp line
  (in-range passes through untouched); `500` → 200 +
  `clamped manualfocus from 500.0 to 7.0 (range -7.0..7.0)`; `-500` → 200 +
  clamped to `-7.0`; `10000` (the inclusive model bound) → 200 + clamped to
  `7.0`, showing both layers doing their separate jobs; `100000` → 422 at the
  model with nothing reaching the camera. The body's reported range
  (`-7.0..7.0`) matches what `tests/fakes/fake_camera.py` assumes.
- **Not verifiable on this body:** the a7 IV exposes **20 settings, all
  `choice`** — no RANGE widgets at all in `INCLUDE_SECTIONS` — so the settings
  slider path through `set_setting` has nothing to exercise it here, and neither
  does the NaN branch (`/api/focus` can't reach it: `FocusStep.steps` is an
  `int`, so a NaN is a 422 at the model). Both are unit-covered only, until a
  camera with sliders is tested.
- **NaN:** rejected with `ValueError` (→400) rather than clamped. `max(low, nan)`
  returns `low`, so an unchecked clamp would have silently driven the lens to one
  end of its travel — and `SettingValue.value` accepts a float, which Python's
  JSON parser will happily produce from a bare `NaN` token.
- **Test:** promoted out of `test_known_gaps.py` into
  `tests/test_gp2_helpers.py::RangeClamping` (bounds, infinities, NaN, per-widget
  ranges, non-RANGE widgets untouched) plus end-to-end coverage in
  `test_gp2_camera.py::ManualFocus` and `::SetSetting`. Verified to fail against
  the pre-fix code (12 failures).
- **To verify on the rig:** `POST /api/focus {"steps": 500}` — inside the model
  bound, far outside the a7 IV's ±7 — should return 200, move the lens by one
  full step, and log `clamped manualfocus from 500.0 to 7.0`. `{"steps": 100000}`
  should now 422 at the model instead. Also confirm a settings slider clamps:
  `POST /api/settings/burstnumber {"value": 9999}`.
- **Still open:** the widget's `step` granularity is ignored — we clamp to
  `low..high` but never snap to the grid, so an off-grid value like `3.5` on a
  step-1 widget is still sent as-is. Unknown whether the a7 IV rounds or rejects;
  worth checking during the next rig session.
- **Where:** `app/app.py:24-25` (`FocusStep`), `camera/gp2.py:189-195`,
  `camera/gp2.py:296-301` (`_coerce`)
- **Issue:** `FocusStep.steps` is an unbounded `int`. `_coerce` does `float(value)`
  with no reference to the widget's advertised `get_range()`. Per the hardware
  notes, `manualfocus` on the a7 IV is a RANGE bounded **−7..7**;
  `POST /api/focus {"steps": 100000}` goes straight to the lens motor. The same
  gap applies to every range slider routed through `set_setting`.
- **Why it matters:** Relying on libgphoto2 or the body's firmware to clamp is
  delegating hardware protection to a layer that never promised it — the software
  equivalent of writing past an actuator's soft limits and hoping the driver
  notices. Clamping in `_coerce` fixes `/api/focus` **and** every settings slider
  in one place.
- **Fix:** In `_coerce`, take the widget (not just its type), read
  `lo, hi, step = widget.get_range()`, and clamp. Bound `FocusStep.steps` with a
  `Field(ge=…, le=…)` as defence in depth.

### 6. ✅ FIXED — `POST /api/settings/{name}` can write *any* widget in the tree

- **Status:** Fixed and **verified on hardware** 2026-07-25.
  `_settable_widgets(config)` is now the single
  definition of "a setting" and both paths use it: `list_settings` describes what
  it yields, `set_setting` resolves the name through `_settable_widget` against
  the same generator. The allowlist can no longer drift because there is only one
  of it. An unmatched name raises `KeyError`, which `app/app.py` maps to **404**
  (distinct from 400 "bad value") without dropping the connection.
- **Blast radius, measured 2026-07-25:** on the a7 IV this takes the writable
  surface from every widget in the tree down to the **20 `choice` widgets** the
  listing offers. Read-only widgets, `GP_WIDGET_BUTTON`s, everything in `status`,
  and every drive in `actions` — `bulb`, `movie`, `autofocus`, `manualfocus`,
  `spotfocusarea` — are now unreachable through this endpoint.
- **Test:** promoted out of `test_known_gaps.py` into
  `tests/test_gp2_camera.py::SetSetting`, asserted as a round trip rather than a
  list: every name `list_settings` returns is writable, nothing else is, and a
  refused write reaches no widget at all. Route-level 404 covered in
  `test_app_routes.py::Settings`. Verified to fail against the pre-fix code.
- **Verified on the a7 IV 2026-07-25, with a live before/after on the identical
  request.** Against the pre-fix service, `POST /api/settings/bulb {"value": 1}`
  returned **200**, fired the shutter, and the refreshed listing came back with
  **2 widgets instead of 20** — the body had gone busy and dropped nearly its
  whole config surface, so the endpoint returned a near-empty settings panel as
  a side effect of taking a photograph. Against the fixed service the same
  request returns `404 {"detail":"no settable setting named 'bulb'"}` and touches
  nothing. Also 404: `manualfocus` (an `actions` widget) and `batterylevel` (a
  `status` widget). `shuttertype` — a genuine listed setting — still returns 200
  with the full 20-item list, confirming the allowlist is not drawn too tight.

- **Where:** `app/app.py:271-281` → `camera/gp2.py:235-241`
- **Issue:** `list_settings()` carefully filters to `INCLUDE_SECTIONS` and drops
  read-only widgets (`gp2.py:224-233`). `set_setting()` re-checks **neither** — it
  calls `cfg.get_child_by_name(name)` on the whole config tree with a
  client-supplied name.
- **Why it matters:** An unauthenticated client on the AP can write widgets the UI
  never exposed. Depending on body and driver version, libgphoto2's PTP tree
  includes raw opcode/passthrough widgets — turning "web UI setting change" into
  "arbitrary PTP command to the camera." The read path already defines the correct
  allowlist; the write path just isn't using it.
- **Fix:** Resolve the name through the same filter the listing uses and 404 on
  anything else:
  ```python
  def _settable_widget(self, cfg, name):
      for section in cfg.get_children():
          if section.get_name() not in INCLUDE_SECTIONS:
              continue
          for w in _walk(section):
              if w.get_name() == name and w.get_type() in _KIND and not w.get_readonly():
                  return w
      raise KeyError(name)
  ```

---

## Tier 2 — Robustness of the camera layer

### 7. 🧪 `_drain_events` can spin forever holding the lock

- **Status:** Fix applied 2026-07-26 — **not yet verified on hardware.**
  `_drain_events(timeout=DRAIN_TIMEOUT, poll_ms=DRAIN_POLL_MS)` now runs against
  a `time.monotonic()` deadline (1.0s, polling in 200ms slices) instead of
  looping until the body volunteers a `GP_EVENT_TIMEOUT`. On expiry it logs
  `WARNING event queue still busy after 1.0s — continuing with events pending`
  and returns, so the lock is always released and the capture proceeds. A
  `GPhoto2Error` while draining is still swallowed — deliberately, see below —
  but now logged: `WARNING` if `is_disconnect_error()` classifies it, `DEBUG`
  otherwise. Unit-covered by `tests/test_gp2_camera.py::DrainEvents` (9 tests),
  including `test_the_bound_holds_on_the_real_clock`, which runs the drain
  against the genuine `time` module rather than `FakeClock` — without it, a fake
  that stopped charging for polls would make the drain tests *hang* instead of
  fail.
- **Suite status:** green on the Pi 2026-07-26 under `.venv/bin/python` —
  `Ran 251 tests in 1.328s … OK (expected failures=2)`, **zero skips**. That
  confirms the change is sound against the real bindings and broke nothing
  (the fake-device clock accounting it touches underpins the route tests too).
  It says nothing about the camera: no unit test can tell you whether the α7 IV
  ever streams the events this bound exists for. That is the rig question below.
- **Why the drain still swallows transport errors:** the drain is best-effort
  hygiene, not an operation the caller asked for. The capture/bulb that follows
  hits the same dead bus and raises there, where the error can be attributed to
  a request — and `_capture_with_retry` calls the drain *between* attempts, so
  raising would abort a retry that might otherwise succeed. Verified by
  `test_a_drain_failure_is_left_for_the_operation_that_follows`: the disconnect
  still reaches the caller as a 503, just from the capture rather than the drain.
- **To verify — read this order, the first step decides whether the rest is
  meaningful.** A clean capture with no warning is *not* a pass on its own: if
  this body never streams events, the drain never spun pre-fix either and you
  have measured nothing. That is the shape of the void run under #3.
  1. **Census — is the α7 IV noisy at all?** Needs the USB claim, so stop the
     service. Spin the rear/front command dial (in M), the exposure-comp dial
     and the mode dial continuously for the whole window:
     ```bash
     sudo systemctl stop pathfinder
     gphoto2 --wait-event=20s        # prints each event as it arrives
     sudo systemctl start pathfinder
     ```
     Property-change events appearing → the hazard is real here, continue to 2.
     Only timeouts for 20s → **record that and stop**: the bound is defensive on
     this body, #7 stays 🧪 as "not reproducible on the α7 IV," and that same
     answer tells you whether #3's orphaned-`FILE_ADDED` desync can happen here.
  2. **Reproduce the hang, then show it gone.** Emulate the pre-fix code with a
     one-line edit rather than a revert — the constant *is* the difference. Set
     `DRAIN_TIMEOUT = 3600.0` in `camera/gp2.py`, `sudo systemctl restart
     pathfinder`, start spinning the dial, keep spinning, then
     `time curl -s -X POST localhost:8080/api/capture`. Pass = **it hangs**;
     that is the bug. Ctrl-C on curl won't free the worker —
     `sudo systemctl restart pathfinder` to recover, and don't leave 3600 in
     place, since every other route parks behind the held lock. Restore `1.0`,
     restart, repeat the same dial-spinning: the capture should complete, at
     worst ~1s slower than baseline, logging `event queue still busy after 1.0s`.
  3. **The disconnect branch** (new logging from this fix; batch with #2's
     USB-pull session). Close the browser tab first — its telemetry polling will
     drop the camera before your request lands. Pull the cable while idle, then
     `curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/api/capture`.
     The drain is the first bus call in `capture()`, so expect
     `WARNING draining events failed: …` → `camera connection lost … dropping`
     → **503**, and reconnect within ~3s of replugging. If `wait_for_event`
     returns a clean timeout on a dead handle instead of raising, you get the
     503 with no drain line — inconclusive for this branch, not a failure.
  4. **Baseline/regression, service running:** several ordinary captures,
     including back-to-back ones (`shot_gap` 1.5s, the case most likely to find
     leftover events). Pass: normal wall time, `ok:true`, and **no**
     `events pending` line. That warning appearing in ordinary use means 1.0s is
     too tight for this body and `DRAIN_TIMEOUT` belongs in the quirk table as
     per-body timing, not a module constant.

  Watch throughout with:
  ```bash
  journalctl -u pathfinder -f | grep --line-buffered -E 'drain|events pending|connection lost'
  ```
- **Still open:** giving up leaves events queued, which is the same event-stream
  desync #3 describes — one orphaned `FILE_ADDED` can still be consumed by a
  later capture's drain. Bounding the wait stops the hang; it doesn't make the
  queue coherent. The real fix is to match events to the operation that caused
  them rather than flushing blindly.
- **Where:** `camera/gp2.py:146-151`
- **Issue:**
  ```python
  while self._cam.wait_for_event(timeout_ms)[0] != gp.GP_EVENT_TIMEOUT:
      pass
  ```
  No iteration cap, no deadline. A body emitting property-changed events
  continuously (Sony bodies do this while a dial is turned, and in some live-view
  states) never returns `GP_EVENT_TIMEOUT`, so this never exits — with `_lock`
  held. The bare `except gp.GPhoto2Error: pass` also swallows a real `-52`
  disconnect here; it works out only because the following capture re-raises it.
- **Why it matters:** An unbounded wait on a device-driven condition is a hang
  waiting to happen. It's the same reason you never poll a status register without
  a timeout — the device is allowed to misbehave, your loop is not.
- **Fix:** Add a wall-clock deadline (~1s) and return when it expires. Log, rather
  than silently swallow, errors that `is_disconnect_error()` would classify.

### 8. 🧪 No operation can ever time out, and there is no watchdog

- **Status:** Fix applied 2026-07-27 — **not yet verified on hardware.** Three
  parts, matching the three ways a wedge propagated:
  1. **Bounded bus acquisition.** Every `with self._lock:` in `gp2.py` is now
     `with self._bus(name):`, which acquires with a `BUS_TIMEOUT` (2s) deadline
     and raises `CameraBusy` — naming the operation that holds the bus — instead
     of parking the caller's threadpool worker. `_run_camera` maps it to **409**,
     and it is deliberately *not* a disconnect error, so a busy bus never drops a
     healthy connection. `preview()` uses a shorter `PREVIEW_BUS_TIMEOUT` (0.25s),
     since a liveview frame queued behind a capture is stale by the time it lands;
     the MJPEG loop treats a 409 as "pause", not "stream over".
  2. **Bounded connect-lock acquisition.** `_connect_if_needed` takes
     `_connect_lock` via `wait_for(..., CONNECT_TIMEOUT)` and returns `False`
     rather than queueing behind a wedged handshake, so the watcher survives it
     and `/api/connect` answers 503 instead of hanging.
  3. **systemd watchdog.** `WatchdogSec=30` + `NotifyAccess=main` in the unit,
     `Restart=on-failure` → `Restart=always`, and a `_watchdog` task pinging
     `WATCHDOG=1` over `$NOTIFY_SOCKET` at half the deadline (no `python-systemd`
     dependency — the protocol is one datagram). The ping is **earned**: each
     interval must be paid for by a round trip through the same threadpool the
     camera ops use, so an exhausted pool — event loop idle and healthy, which is
     exactly the failure mode — withholds the ping and systemd restarts the
     process. Detection latency is up to ~2 intervals plus the deadline (~45s
     worst case).
  Unit-covered by `tests/test_gp2_camera.py::BusTimeout`,
  `tests/test_app_routes.py::BusyHandling`/`ConnectLock`, and
  `tests/test_watchdog.py` — full suite green in the Pi's venv 2026-07-27
  (295 tests, nothing skipped, so the heartbeat and route tests ran against the
  real FastAPI/anyio rather than being skipped as they are on the dev host).
  That covers the *logic*; the systemd behaviour below is still unverified.
- **Residual risk (unfixable in software):** a blocking C call cannot be
  interrupted from Python, so the *stuck operation itself* is never cancelled —
  it keeps its worker until libgphoto2 returns or the process is restarted. What
  is bounded is everything queued behind it. A `close()` that cannot take the bus
  now logs `ERROR … the USB claim stays open` and raises rather than blocking;
  the claim is released when the holding operation finishes and the handle is
  collected.
- **To verify:** on the Pi — (a) start a long bulb and confirm a concurrent
  `/api/capture` returns 409 `camera is busy with bulb` within ~2s while liveview
  resumes on its own afterwards; (b) `systemctl show pathfinder -p WatchdogUSec
  -p Restart` and `journalctl -u pathfinder | grep "watchdog armed"`;
  (c) `kill -STOP` the process and confirm systemd aborts and restarts it within
  ~30s; (d) confirm no spurious restarts over a long idle session.
- **Where:** whole stack — `camera/gp2.py` (all `self._cam.*` calls),
  `app/app.py:110-117` (`_run_camera`), `tools/setup.sh:143` (`Restart=on-failure`)
- **Issue:** `cam.init()`, `capture()`, `file_get()`, `set_config()` are blocking C
  calls into USB with no bound. If the camera wedges mid-PTP transaction — which
  Sony bodies demonstrably do, per the existing `-52` self-healing — the
  threadpool worker is gone permanently. `run_in_threadpool` uses AnyIO's default
  limiter of **40 workers**; each hang burns one until the process is dead. A
  wedged `_try_connect` (`app/app.py:58`) is worse: it also holds `_connect_lock`, so
  `/api/connect` and the reconnect watcher hang with it.
- **Why it matters:** The failure mode is a silently bricked field device.
  `Restart=on-failure` cannot help, because a hung process hasn't failed — it's
  alive and doing nothing. There is no recovery short of a physical power cycle by
  someone standing next to it.
- **Fix (two parts, both cheap):**
  1. Bounded lock acquisition — `self._lock.acquire(timeout=2.0)` — returning
     **409 busy** instead of parking a worker. This alone prevents most pile-up.
  2. `WatchdogSec=30` in the unit file plus an `sd_notify` heartbeat task. Turns
     "hung forever" into "restarts in 30s."
  Also change `Restart=on-failure` → `Restart=always`.

### 9. 🟡 `_drop_camera` can close a freshly-reconnected camera

- **Where:** `app/app.py:99-107`
- **Issue:** Read-then-clobber with no identity check:
  ```python
  old = app.state.camera
  app.state.camera = None
  ```
  Sequence: request A fails with `-52` and drops the camera; the watcher reconnects
  ~3s later; request B — blocked behind a slow op and carrying the *same* stale
  `-52` — then reads `app.state.camera`, finds the **new healthy** connection, and
  closes it. The `if old is None: return` guard only covers B landing inside the
  3-second reconnect window; anything holding the lock longer than
  `CAMERA_POLL_INTERVAL` (a bulb, a slow `set_config`) opens the window wide.
- **Why it matters:** Classic check-then-act on shared state: the read and the
  write have to be one atomic decision, or a stale observation authorises a
  destructive action. Symptom is a mystery disconnect right after a successful
  reconnect — very hard to reproduce deliberately.
- **Fix:** Compare-and-swap. Have `_run_camera` capture the camera it used and
  forward it:
  ```python
  async def _drop_camera(exc, cam):
      if cam is None or app.state.camera is not cam:
          return                      # already reaped and replaced
      app.state.camera = None
      ...
  ```

### 10. 🟡 Liveview is a per-client stream against a single-owner resource

- **Where:** `app/app.py:167-195`, `web/script.js:28-43`
- **Issue:** Each `<img src="/api/liveview">` opens its own generator grabbing the
  camera lock up to 30×/s, one threadpool task per frame. Three consequences:
  - Multi-client is the stated use case (phone + laptop on the AP), but N clients
    contend for one USB bus. The camera can't sustain 30fps PTP preview anyway;
    the loop just runs as fast as libgphoto2 allows and starves capture.
  - The `bulbing` / `recording` guards are **client-side only**. A second tab has
    `bulbing === false` and keeps requesting frames during a 300s bulb, each
    blocking a threadpool worker on the lock for the whole exposure. ~40 of those
    and the server — including `/api/status` — stops responding.
  - During recording, `preview()` raises `RuntimeError` (`gp2.py:142`), caught by
    the generic handler at `app/app.py:180`, which sleeps 0.3s and **retries forever**
    — a second client hammers the lock at 3.3 Hz for the whole take.
- **Why it matters:** The camera is a single-owner resource with one bus; the
  current design lets client count determine hardware contention. Server-side
  state must be the authority on when the bus is busy.
- **Fix (structural):** One shared frame producer — a background thread pulling
  previews, fanning the latest frame out to all subscribers. Makes client count
  irrelevant and lets bulb/record pause production server-side.
- **Fix (minimum viable):** An `app.state.busy` flag set by bulb/record, with
  liveview and telemetry returning 409 immediately instead of blocking.

### 11. 🟡 Capture filenames: traversal, backwards clocks, and non-atomic writes

- **Where:** `camera/gp2.py:123-128`
- **Issue:** `target = os.path.join(save_dir, f"{int(time.time())}_{path.name}")`
  has three independent problems:
  - **Traversal from device data.** `path.name` comes from the camera. The
    timestamp prefix defuses a *leading* `../`, but not an embedded one — a name
    like `a/../../../x` resolves outside `captures/`.
  - **No RTC on the Pi.** In AP mode there's no internet, so no NTP;
    `int(time.time())` comes from `fake-hwclock` and **can go backwards** after a
    reboot. Combined with camera counters that reset on card format, `.save()`
    silently overwrites existing captures.
  - **No free-space check, non-atomic write.** A full SD card leaves a truncated
    file that looks like a valid capture.
- **Why it matters:** The USB port is a trust boundary on a device intended to
  accept ~2,000 camera models — device-supplied strings are untrusted input, and
  hardening is one line. The clock and atomicity issues are silent data loss on a
  device whose entire purpose is not losing the photo.
- **Fix:** `os.path.basename(path.name)` plus a `[A-Za-z0-9._-]+` whitelist; a
  monotonic per-session sequence number instead of (or alongside) the wall clock;
  write to `.part` and `os.rename()` (atomic within a filesystem, so a reader never
  sees a half-file); check free space before starting.
- **Related:** compounds with the roadmap's capture-review item — captures
  currently accumulate with no way to list or delete them from the UI, so a field
  device fills up and starts failing with no visible cause.

### 12. 🟡 `self.recording` is app-side belief, not camera truth

- **Where:** `camera/gp2.py:166-177`
- **Issue:** `set_recording` writes the widget and unconditionally sets the flag.
  If the body silently refuses (no card, wrong mode dial), the flag says
  "recording" and stills stay blocked until the user presses Stop. After a
  mid-record disconnect, `Gphoto2Camera.__init__` resets it to `False` while the
  camera is still rolling.
- **Why it matters:** State that models hardware without reading hardware back
  drifts, and the UI presents the drifted value as authoritative.
- **Fix:** Read the widget back after the write and derive the flag from the
  camera's reported value. On reconnect, query rather than assume.

---

## Tier 3 — Structure & future camera support

### 13. 🟠 Vendor quirks replace the defaults instead of layering over them

- **Where:** `camera/gp2.py:304-313` (`_quirks_for`), `camera/gp2.py:14-29`
  (`DEFAULT_QUIRKS`), `camera/sony.py:1-15` (`GENERAL`)
- **Issue:** `_quirks_for` returns the vendor dict as-is, so `sony.GENERAL` has to
  re-specify all 13 keys — duplicating `DEFAULT_QUIRKS` in full.
- **Why it matters:** **This is the most important structural fix before a second
  vendor lands.** When `canon.py` omits a key, there is no error at import — you
  get a `KeyError` deep inside a request handler, months later, on someone else's
  device. The duplication also guarantees drift: the two tables will disagree.
- **Fix:** `return {**DEFAULT_QUIRKS, **q}`, and assert at import that no vendor
  introduces a key absent from `DEFAULT_QUIRKS` (catches typos like `af_widgets`).
  Then strip `sony.GENERAL` down to only what actually differs.
- **Test:** acceptance test waiting at
  `tests/test_known_gaps.py::QuirkLayering` (expected-failure). The "no unknown
  keys" half is already enforced by
  `tests/test_gp2_helpers.py::QuirkResolution`.

### 14. 🟡 No declared vendor contract

- **Where:** `camera/gp2.py:30` (`VENDORS = [sony]`)
- **Issue:** The backend duck-types a module-level `quirks(model)` function. What a
  vendor module owes the backend is documented in prose only.
- **Why it matters:** This is the first file a contributor adding Nikon reads. An
  explicit contract is both documentation and a test surface.
- **Fix:** A `Protocol` (or a small registry decorator) plus a validation pass over
  each registered vendor at import.

### 15. 🟡 Model matching is fragile, and the per-model layer has never been exercised

- **Where:** `camera/sony.py:17-30`
- **Issue:** `quirks()` gates on `"sony" in model.lower()`, then substring-matches
  `MODELS`. But `MODELS = {"A7 IV": {}}` has an **empty override dict** — so the
  entire per-model layer is currently a no-op and has never actually run.
  Separately, matching a marketing string is brittle across driver versions; the
  project notes already record the `(Control)` vs `(PC Control)` suffix biting.
- **Why it matters:** Untested code paths that look load-bearing are worse than
  absent ones. And a body's USB VID/PID is stable identity — the model string is a
  human-readable label that upstream is free to reword.
- **Fix:** Key on VID/PID from `get_abilities()` with the model string as fallback.
  Add a test that asserts the per-model layer actually applies an override.

### 16. 🟡 `_ensure_focus_mode` writes a single hardcoded target and never restores it

- **Where:** `camera/gp2.py:206-217`, `camera/sony.py:8-11`
- **Issue:** Two problems:
  - It writes one literal target (`af_target_mode`, `"AF-A"`). If a body doesn't
    offer that exact string, **every AF press 400s permanently.**
  - Pressing AF silently mutates the camera's focus mode and never restores it. On
    Sony bodies focus mode also has physical switch interactions, so the driver may
    accept a write the hardware overrides.
  Also, `af_modes` lists `"AF-S"`, which the recorded a7 IV `focusmode` choices
  (`Automatic/AF-A/AF-C/DMF/Manual`) do not contain — harmless as an extra
  "acceptable" entry, but it signals the table is partly guessed.
- **Why it matters:** A hardcoded string is a single point of failure across ~2,000
  supported bodies, and the failure is total (the button never works) rather than
  degraded.
- **Fix:** Choose the first entry of `acceptable` that is actually present in the
  widget's `get_choice(...)` list, rather than trusting one literal. Document — or
  restore — the focus-mode mutation.

### 17. 🟠 Verify three Sony quirk values against the real body

- **Status:** Two of three **resolved from a full `--list-all-config` dump**
  2026-07-30, now checked in at `tests/fixtures/ilce_7m4.txt`. The remaining
  sub-item needs the rig.
  - ✅ **`af_area_widget` was wrong and is fixed.** Confirmed: `changeafarea`
    appears **nowhere** in the α7 IV tree. The real widget is
    `/main/actions/spotfocusarea` (`TEXT`, writable). Tap-to-focus had never
    worked — `_drive_action` would raise `[-2] Bad parameters` — and the tests
    were green because the fake published `changeafarea` too. Fixed in
    `camera/sony.py`, with the fake rebuilt from the dump and
    `test_fake_fidelity.TheQuirksMatchTheHardware` added to assert every quirk
    widget name against real hardware output. Verified to fail against the
    pre-fix quirk table.
  - ✅ **`bulb_widget: "bulb"` is correct on this body.** `/main/actions/bulb`
    exists, `TOGGLE`, writable, label "Bulb Mode". The Canon-convention worry was
    unfounded here. (The *friendly-message* half of this item still stands — the
    `if not widget` guard at `gp2.py:141` only checks the quirk is set, not that
    the widget exists, so a body without it still gets a raw libgphoto2 error.)
  - ⬜ **The idle-value question is open, and it is broader than AF.** The dump
    shows **both** `autofocus` *and* `bulb` reporting `Current: 2`, while
    `af_drive_values` releases AF to `0` and `bulb()` releases to `0`. So both
    drives leave the widget in a state the body may not consider idle. Needs the
    rig: read the value back after a release and see whether the body resets it
    to 2 on its own.
- **Remaining fix:** determine `af_area_size` empirically — the dump cannot
  settle it, because `spotfocusarea` is a `TEXT` widget with an empty current
  value and advertises no range. Set `af_area_size` to `(1, 1)`, tap each corner
  of the preview, and read back what the body accepted. Also confirm the point
  only moves when `focusarea` is one of the `Flexible Spot` choices.

### 18. 🟡 Whole-tree config writes where single-widget writes belong

- **Where:** `camera/gp2.py:235-241` (`set_setting`), `gp2.py:206-217`
  (`_ensure_focus_mode`), `gp2.py:166-177` (`set_recording`) — versus
  `gp2.py:219-222` (`_drive_action`), which does it correctly
- **Issue:** These read the entire config tree and write it back, rather than using
  `get_single_config` / `set_single_config`.
- **Why it matters:** Whole-config writes are slow on Sony (hundreds of ms to
  seconds) and are the classic source of "the camera changed a setting I didn't
  touch." The codebase already has the right pattern in `_drive_action` — this is
  an internal inconsistency, not an unknown.
- **Fix:** Move all three to the single-config path.

### 19. ⚪ Every setting change costs three full config reads

- **Where:** `app/app.py:271-281`
- **Issue:** `set_setting` does a full `get_config`, then the handler returns
  `cam.list_settings()` which does another.
- **Why it matters:** Combined with the 400ms debounce and range `change` events,
  the UI will feel laggy and will hog the USB bus that liveview and capture need.
- **Fix:** Return only the changed widget's descriptor, or cache the config tree.

### 20. 🟠 A camera in MTP/Mass Storage mode connects "successfully" but does nothing

- **Where:** `camera/gp2.py:316-324` (`connect`)
- **Issue:** If a Sony body is in MTP/Mass Storage rather than PC Remote,
  `init()` **succeeds** but the config tree is nearly empty.
- **Why it matters:** The user sees `Connected: <model>` with a blank settings
  panel and every button returning 400 — the single most confusing possible
  failure, and one every new user will hit at least once.
- **Fix:** A sanity check at connect ("did we get any settable widgets?") that
  surfaces a specific message: *"Camera connected but reporting no controls — check
  USB mode is set to PC Remote."*

---

## Tier 4 — Security posture

The threat model — a physically-present operator on a private AP — is reasonable.
These are the places it's weaker than it looks.

### 21. 🟠 Every device ships with the same AP password

- **Where:** `tools/setup.sh:12` (`AP_PASS="pathfinder"`), published in `README.md:24`
- **Issue:** A hardcoded, documented, 11-character PSK identical across all units.
- **Why it matters:** Anyone in WiFi range gets full camera control — including the
  arbitrary widget writes of #6. A published default credential is not a
  credential.
- **Fix:** Generate a random per-device PSK during provisioning and print it once
  at the end of `tools/setup.sh`.

### 22. 🟡 No authentication, binds `0.0.0.0:8080`

- **Where:** `tools/run.py:14`
- **Issue:** Fine behind the AP. Not fine in the `AP_ON_BOOT=0` development mode the
  README explicitly recommends (`README.md:87`), where the entire home LAN gets
  unauthenticated shutter control.
- **Why it matters:** The safe deployment and the documented dev workflow have
  different threat models, but identical code.
- **Fix:** At minimum bind to the AP interface only when the AP is up. A shared
  token in a query string or header would cover the dev case.

### 23. 🟡 CSRF on the body-less POST endpoints

- **Where:** `app/app.py:138` (`/api/capture`), `app/app.py:210-217` (`/api/record/*`)
- **Issue:** These take no request body, so a plain auto-submitting
  `<form action="http://10.42.0.1:8080/api/record/start" method="post">` on any
  website fires them cross-origin. The JSON-body endpoints are incidentally
  protected — a cross-origin *simple* request can't set
  `Content-Type: application/json`.
- **Why it matters:** Low harm (an attacker trips your shutter), but it's an
  unintended write path from the open internet into the device, and the fix is one
  line.
- **Fix:** Require a custom header on state-changing routes, or validate
  `Origin`/`Host`.

### 24. 🟡 The service runs as the login user

- **Where:** `tools/setup.sh:138-142`
- **Issue:** `User=$USER` — typically a member of `sudo`. No hardening directives.
- **Why it matters:** Any RCE in the app is effectively root on the device.
- **Fix:** A dedicated unprivileged `pathfinder` user in `plugdev` only, plus
  `NoNewPrivileges=true`, `ProtectSystem=strict`, `PrivateTmp=true`, and
  `ReadWritePaths=` for the capture directory. About six lines.

### ✅ Verified clean — no action needed

- **No XSS.** Every DOM write in `web/script.js` uses `textContent` or
  `new Option()`; no `innerHTML` anywhere. Camera-supplied labels and choices are
  handled safely.
- **`.vscode/sftp.json` holds a plaintext SSH password** but is correctly
  gitignored and was **never committed** on any branch (verified against full
  history). Awareness only.

---

## Tier 5 — Deployment & provisioning

### 25. 🟠 libgphoto2 is built from an unpinned `master`

- **Where:** `tools/setup.sh:40` (library), `tools/setup.sh:59` (CLI);
  `tools/requirements.txt`
  (lower bounds only)
- **Issue:** Every device provisioned on a different day gets a different library
  build. Python deps have the same problem — `fastapi>=0.110` resolves to whatever
  is newest at provision time.
- **Why it matters:** When an upstream commit breaks Sony support, you'll have
  units in the field that work and units that don't, **with no way to tell them
  apart.** Reproducible provisioning is the difference between "we shipped a known
  build" and "we shipped whatever was on master that morning."
- **Fix:** Pin a tag or commit SHA for both repos. Pin exact versions in
  `tools/requirements.txt` (or add a lockfile), and record the built library version
  somewhere queryable at runtime.

### 26. 🟡 `tools/setup.sh` re-run hazards

- **Where:** `tools/setup.sh:28`, `:38`, `:57`, `:78`
- **Issue:**
  - `sudo apt full-upgrade -y` runs on *every* re-run and can break a working field
    device mid-update.
  - `git pull --ff-only || true` silently continues with stale source on failure.
  - `apt remove --purge` of the libgphoto2 packages can cascade into dependents
    (gvfs, etc.).
- **Why it matters:** The script is documented as safely re-runnable
  (`README.md:74`), which invites running it on a device that currently works.
- **Fix:** Separate "provision" from "update." Drop the blanket upgrade from the
  re-run path, fail loudly on a failed pull, and `--dry-run` the purge first.

### 27. 🟡 Logging defaults to DEBUG — real SD card wear

- **Where:** `logs/log.py:4` (`DEFAULT_LEVEL = "DEBUG"`)
- **Issue:** On a Pi writing to persistent journald, with a 3-second reconnect poll
  (`app/app.py:51`) and per-frame liveview debug lines (`app/app.py:181`).
- **Why it matters:** Flash endurance is a hard constraint on this platform, not a
  theoretical one — this is a wear-out failure with a slow fuse.
- **Fix:** Default to `INFO`. Consider `Storage=volatile` in journald, or a
  size-capped persistent journal.

### 28. ⚪ `logs/` is a dead directory

- **Where:** `logs/debug.log`, `logs/log.py:14`
- **Issue:** `configure_logging` only calls `basicConfig` (stderr → journald).
  Nothing writes `logs/debug.log`; it's a stale artifact of an earlier design.
- **Fix:** Delete it, or wire up a real file handler if one is actually wanted.

---

## Tier 6 — Smaller items

### 29. ⚪ `os.environ.setdefault("LD_LIBRARY_PATH", ...)` is a no-op

- **Where:** `app/app.py:13`
- **Issue:** glibc reads `LD_LIBRARY_PATH` at process start; setting it in-process
  does not affect later `dlopen` search paths. It works today only because of
  `tools/setup.sh:82` (`ld.so.conf.d`) and the systemd `Environment=` line
  (`tools/setup.sh:141`).
- **Why it matters:** It makes the `import camera` placement below it look
  load-bearing. Someone will "fix the lint" by hoisting the import and conclude
  nothing broke — which is true, but for the wrong reason.
- **Fix:** Remove the line; add a comment noting the real mechanism.

### 30. 🟡 Blocking USB I/O on the event loop during startup and shutdown

- **Where:** `app/app.py:71` (`_try_connect`), `app/app.py:80` (`set_recording`),
  `app/app.py:84` (`camera.disconnect`)
- **Issue:** All three run directly on the event loop, not via
  `run_in_threadpool`.
- **Why it matters:** Startup blocks the server on the USB handshake; shutdown can
  hang until systemd SIGKILLs at 90s, which skips the "stop recording" cleanup that
  block exists to perform.
- **Fix:** Wrap in `run_in_threadpool` with an `asyncio.wait_for` timeout.

### 31. ⚪ CWD-relative paths

- **Where:** `app/app.py:284` (`StaticFiles(directory="web")`), `camera/gp2.py:12`
  (`CAPTURE_DIR = "captures"`)
- **Issue:** Correct only because the systemd unit sets `WorkingDirectory`. Running
  `python tools/run.py` from anywhere else crashes at import or writes captures to a
  surprising location.
- **Fix:** `Path(__file__).parent / "web"`, and resolve `CAPTURE_DIR` against the
  package root.

### 32. 🟡 A watcher crash silently ends all reconnection

- **Where:** `app/app.py:61-64`
- **Issue:** If `_camera_watcher`'s body ever raises, the task dies. Nothing logs
  it and nothing restarts it.
- **Why it matters:** The device would appear permanently "no camera connected"
  with no diagnostic — the reconnect feature would be gone with no trace.
- **Fix:** `try/except` inside the loop, log, and continue.

### 33. ⚪ Inconsistent error mapping across routes

- **Where:** `app/app.py:259-268` (`/api/telemetry`, `/api/settings`), `app/app.py:138-145`
  (`/api/capture`)
- **Issue:** `/api/telemetry` and `/api/settings` have no generic handler, so
  gphoto2 errors become **500 + traceback**, while every sibling route maps them to
  400. `/api/capture` lacks the `except Exception` that `/api/bulb` has.
- **Fix:** Factor the shared error mapping into one decorator or helper so every
  route behaves the same way.

### 34. ⚪ `_capture_with_retry` can return `None`

- **Where:** `camera/gp2.py:153-164`
- **Issue:** If a quirk sets `capture_retry_attempts <= 0`, the loop body never
  runs and the function falls off the end returning `None` →
  `AttributeError: 'NoneType' object has no attribute 'name'` in `_download`.
- **Why it matters:** A plausible mistake in a future vendor file, surfacing as a
  confusing crash far from its cause.
- **Fix:** Validate the quirk value (`max(1, attempts)`) or raise explicitly.
- **Test:** acceptance test waiting at
  `tests/test_known_gaps.py::RetryBounds` (expected-failure).

### 35. ⚪ Settings panel re-renders mid-interaction

- **Where:** `web/script.js:284-294` (`applySetting`)
- **Issue:** Every change re-renders the whole panel, destroying the `<select>` or
  slider the user is currently touching. On mobile this closes the picker
  mid-interaction.
- **Fix:** Patch only the changed row, or skip re-render for the element that has
  focus.

### 36. ⚪ Unused dependency

- **Where:** `tools/requirements.txt:3` (`websockets>=12`)
- **Issue:** No WebSocket code anywhere in the tree.
- **Fix:** Remove it.

### 37. ✅ FIXED — No tests

- **Status:** Fixed 2026-07-25. `tests/` — **311 tests** (228 at the time of the
  fix, 251 after #7, 295 before the hardware-fixture suite), stdlib `unittest`,
  no third-party test dependencies. A fake
  `gphoto2` binding (`tests/fakes/`) is installed into `sys.modules` before
  `camera` is imported, so the whole camera layer runs with no libgphoto2 and no
  camera attached (197 of 311 execute on the dev host, which has no pip). Covers
  exactly what this item asked for: quirk resolution, `_coerce`, the
  error→HTTP-status mapping, and the disconnect/reconnect state machine. See
  `tests/tests.md`.
- **Last full run on the Pi, 2026-07-26** (under `.venv/bin/python`, after the
  #7 fix): `Ran 251 tests in 1.328s … OK (expected failures=2)` with **zero
  skips** — so the FastAPI/pydantic tests and the fake-vs-real-binding fidelity
  checks all executed against the genuine binding, not just the dev host's 166.
  The two expected failures are the remaining `test_known_gaps.py` items (#13,
  #34); they were 4 before #5 and #6 were promoted into the main suite.
- **Note:** `tests/test_known_gaps.py` holds an `@expectedFailure` acceptance
  test for each remaining hazard — now #13 and #34, after #5's and #6's were
  promoted. Fixing one makes the suite red with an *unexpected success* — the
  cue to promote the test, not a regression.
- **Still open:** the libgphoto2 `vusb` dummy driver is unused, so nothing
  exercises the real binding end-to-end; there is no CI (the suite needs only
  `fastapi`+`pydantic`, so this is cheap); and the frontend has only static
  contract checks, no runtime tests.

### 38. 🟠 `bulb` reports success when the body is not in BULB mode

- **Where:** `camera/gp2.py` (`bulb`), `app/app.py` (`/api/bulb`)
- **Issue:** `bulb()` refuses only when the *quirk table* has no bulb widget. It
  never checks whether the body is in a state where driving that widget means
  anything. Found on the rig 2026-07-25 while attempting to verify #3: with the
  mode dial in **P**, `POST /api/bulb {"seconds": 30}` drove the widget (which
  acts as a plain shutter release), the camera took a **1s Program-AE** frame,
  the app slept its full 30s, downloaded that frame and returned
  `{"ok":true,"path":…}`. Confirmed by EXIF: `ExposureTime 1/1`,
  `ExposureProgram Program AE`.
- **Why it matters:** A silent lie on a device with no screen. You would come
  back from a night shoot with a card full of 1-second frames believing they
  were 30-second ones — and the API said `ok:true` every time. It also cost a
  full verification round on #3, because a green result looked like a pass.
  Reporting success for work not performed is worse than failing.
- **Fix:** Before driving the widget, read the exposure-mode / shutter-speed
  widget and raise `RuntimeError` (→409) if the body is not in manual + BULB.
  Needs one fact from the rig first: what `shutterspeed` reports when the body
  *is* in BULB —
  `curl -s localhost:8080/api/settings | python3 -m json.tool | grep -iE -A6 '"(shutterspeed|expprogram|exposuremode)"'`
  run once in P and once in BULB.
- **See also:** #17 (rig-verify quirk values), #3 (blocked behind the same dial
  access).

---

## Later findings (second review pass, 2026-07-30)

Found after the watchdog work in `d841aa4`. Numbered in discovery order rather
than slotted into the tiers above, so existing references stay valid — severity
is in the emoji, and the fix order is in "Suggested order" below. #39-#41 are all
consequences of the #8 fix that the docs written alongside it didn't account for.

### 39. 🟠 A wedged USB handshake at boot becomes a silent restart loop

- **Where:** `app/app.py:158` (`_try_connect` in `lifespan`), `app/app.py:160-166`
  (watchdog task creation), `tools/setup.sh:136` (`Type=simple`),
  `tools/setup.sh:143-145`
- **Issue:** `lifespan` calls `_try_connect(app)` **synchronously, before** the
  watchdog task exists. With `Type=simple` systemd starts the `WatchdogSec=30`
  timer at `exec`, not at `READY=1`, so the deadline is already running during
  that blocking `cam.init()`. A body that wedges the handshake at boot is
  SIGABRT'd before the first `WATCHDOG=1` is ever sent, `Restart=always` /
  `RestartSec=3` brings it back, and it wedges again.
- **Why it matters:** The loop has no terminal state. Cycles are ~33s apart, so
  systemd's default `StartLimitBurst=5` / `StartLimitIntervalSec=10s` never
  trips — the unit never reaches `failed`, so nothing escalates and nothing
  alerts. The device just cycles, and the journal shows a restart with no error.
  This is the watchdog firing on the one startup path it cannot protect: the
  window before it is armed. #30 notes the blocking startup and #8 the watchdog;
  the interaction is new as of `d841aa4`.
- **Fix:** Arm the watchdog **first**, then connect off the loop:
  ```python
  tasks = [asyncio.create_task(_camera_watcher(app))]
  interval = _watchdog_interval()
  if interval is not None:
      _sd_notify("READY=1")
      tasks.append(asyncio.create_task(_watchdog(interval)))
  await run_in_threadpool(_try_connect, app)   # after, not before
  ```
  Consider an explicit `StartLimitIntervalSec=0` or a longer `RestartSec` in the
  unit so a genuinely dead device backs off instead of spinning.
- **See also:** #30 (the blocking call itself), #8 (the watchdog).

### 40. 🟡 `_watchdog` is an unguarded task, and it fails *closed*

- **Where:** `app/app.py:134-152`
- **Issue:** Same shape as #32 (`_camera_watcher` dying silently), but the
  consequences are inverted. If the loop body raises — `probe.exception()`
  returning something the code doesn't expect, anything non-`OSError` escaping
  `_sd_notify` — the task dies, the pings stop, and systemd aborts the process.
- **Why it matters:** #32 costs you the reconnect feature. This costs you a
  **healthy** process, killed every 30s forever, with the watchdog reporting the
  outage it is itself causing. A watchdog that fails closed on its own bug is
  worse than no watchdog: the standard rule for a kick-from-the-main-loop design
  is that the kicker must be the most boring code in the process.
- **Fix:** `try/except Exception: log.exception(...)` around the loop body so a
  bug in the heartbeat can't be mistaken for a hang. Fix alongside #32 — same
  pattern, both tasks.

### 41. 🟡 Shutdown during a bulb silently skips the cleanup it exists to perform

- **Where:** `app/app.py:174-183` (`lifespan` teardown)
- **Issue:** Both `set_recording(False)` and `camera.disconnect()` now go through
  `_bus(...)` with the 2s `BUS_TIMEOUT`, and both are wrapped in a bare
  `except Exception: pass`. While a bulb holds the bus (up to
  `MAX_BULB_SECONDS`), each times out with `CameraBusy` and is swallowed with no
  log line: recording is not stopped and the USB claim is not released. uvicorn
  then blocks on the worker thread anyway, so the process lingers regardless.
- **Why it matters:** `tests/tests.md` states the guarantee "Shutdown stops
  recording before it disconnects, so a service restart can't leave the body
  filling the card." That is now conditional on the bus being free, and the test
  passes because it never holds the bus during shutdown. A guarantee that quietly
  became conditional is worse than one that was never claimed. #8's residual-risk
  note covers the claim leaking in general; it does not cover the shutdown path.
- **Fix:** Log at `WARNING` instead of `pass` (a swallowed shutdown failure on a
  headless device is invisible), and give the shutdown path a longer bus deadline
  than 2s so it waits out a short exposure rather than giving up instantly. Add a
  test that holds the bus across shutdown, so the stated guarantee is pinned to
  the condition it actually holds under.

### 42. 🟡 `tools/requirements.txt` installs `gphoto2` twice, contradictorily

- **Where:** `tools/requirements.txt:4`, `tools/setup.sh:106` (step 6/9),
  `tools/setup.sh:109-112`
  (step 7/9)
- **Issue:** Step 6 installs `gphoto2` from `tools/requirements.txt` as a PyPI wheel —
  which **bundles its own libgphoto2** — and step 7 immediately force-reinstalls
  it from source against `/usr/local`. The first install is pure waste on a Pi,
  and the two disagree about which library the binding links.
- **Why it matters:** The failure mode is exactly what steps 2-4 exist to
  prevent. If step 7 is ever skipped, interrupted, or reordered, the tree still
  has a working-looking `import gphoto2` — linked against the bundled library
  with the Sony regression `tools/setup.sh` builds from source to avoid. Two install
  paths for one dependency means the "which libgphoto2 am I actually running"
  question has no single answer, which compounds #25 (unpinned build).
- **Fix:** Drop `gphoto2` from `tools/requirements.txt` (leaving one install path,
  step 7) and note in the file why it is deliberately absent. Optionally have
  step 7 assert the built extension resolves to `/usr/local` afterwards.

### 43. ⚪ `captures/` is not gitignored

- **Where:** `.gitignore`, `camera/gp2.py:14` (`CAPTURE_DIR`), `tools/setup.sh:140`
  (`WorkingDirectory`)
- **Issue:** `CAPTURE_DIR` defaults to `"captures"`, CWD-relative, and the unit
  sets `WorkingDirectory=$PROJECT_DIR` — so every shot lands as an untracked file
  **inside the git working tree**, and inside whatever the `.vscode/sftp.json`
  sync covers. `.gitignore` covers `logs/` but not this.
- **Why it matters:** RAWs in the working tree make `git status` useless on the
  device, invite an accidental `git add -A` committing photos, and put the
  capture directory in the path of an editor sync that has no reason to see it.
- **Fix:** Add `captures/` to `.gitignore`. Better, resolve `CAPTURE_DIR` to a
  path outside the repo by default (`~/pathfinder-captures`, or a
  `ReadWritePaths=` directory once #24 lands) — see #31 for the CWD-relativity
  half of this.

### 44. ⚪ `POST /api/settings/{name}` re-reads outside its own error mapping

- **Where:** `app/app.py:389`
- **Issue:** The trailing `return await _run_camera(cam.list_settings)` sits
  **outside** the handler's `try`. A gphoto2 failure on the read-back is an
  unhandled 500 + traceback, while the identical failure on the write two lines
  above is a clean 400.
- **Why it matters:** The browser re-renders the whole panel from this response,
  so the read-back is not incidental — it is half the endpoint's contract. Same
  family as #33, different line than the ones that item names.
- **Fix:** Fold it into the `try`, or into whatever shared error-mapping helper
  #33 produces.

### 45. ⚪ README tells you to clone over SSH on a machine with no key

- **Where:** `README.md` (Provisioning), `git@github.com:nathanroorda/pathfinder.git`
- **Issue:** A freshly imaged Pi has no SSH key and no GitHub association, so the
  very first provisioning command fails unless the operator has already set up a
  deploy key by hand.
- **Fix:** Use the HTTPS clone URL in the README. *(Fixed 2026-07-30 as part of
  the README drift pass.)*

### 46. ✅ FIXED — Documentation drift outside the README

- **Status:** All four corrected 2026-07-30, in the same pass as the README.
  No code changed — these were documentation-only inaccuracies. `logs/log.md` now
  states stderr and spells out the `2>&1` consequence for running outside
  systemd; `web.md` carries the `!bulbing` term and now lists all three
  `updateLiveview()` call sites, distinguishing *why* the stream is off during a
  recording (backend refuses) from during a bulb (backend accepts but the lock is
  held); `camera.md` attributes the settings/telemetry split to
  `INCLUDE_SECTIONS` rather than the read-only filter; `tools/requirements.txt` points
  at the right filename.
- **Where:** `logs/log.md:8,19-20,45`; `web/web.md:89`; `camera/camera.md:337`;
  `tools/requirements.txt:4`
- **Issue:** Four independent inaccuracies, none behavioural:
  - **`logs/log.md` says stdout three times.** `logging.basicConfig()` with no
    `stream=` uses **stderr** (verified). No impact under journald, which
    captures both — but the claim is wrong, and it matters the moment anyone
    pipes the app or redirects one stream.
  - **`web.md:89`** gives the liveview on-state as `connected && !recording`;
    `script.js:45` and web.md's own line 163 say
    `connected && !recording && !bulbing`. Stale since the bulb feature landed.
  - **`camera.md:337`** attributes the telemetry/settings non-overlap to
    `list_settings`'s not-read-only filter. It is actually the section split —
    `status` is not in `INCLUDE_SECTIONS`, so status widgets never reach that
    filter at all. The real guarantee is *stronger* than the documented one; a
    writable widget in `status` would break the documented mechanism but not the
    actual one.
  - **`tools/requirements.txt:4`** comment points at `camera/gphoto2.py`; the file is
    `camera/gp2.py`.
- **Why it matters:** These docs are precise enough that they get trusted over
  the source. A doc that is right 95% of the time is read as authoritative, so
  the wrong 5% is more dangerous than a vague doc would be.
- **Note:** `TODO.md`'s own stale test counts (#37 said 251/166) were corrected
  in the same pass, and again when the hardware-fixture suite landed; the count
  is now 311/197, matching `tests/tests.md` and a live run.
- **Residual:** nothing enforces this. All four drifted because no test can see
  a prose claim, and three of them (`web.md`, `camera.md`, `#37`'s counts) went
  stale because a *code* change had no corresponding doc edit. The cheap partial
  fix is to extend the `test_web_contract.py` trick — parse the doc, assert
  against the source — for the handful of claims that are mechanically
  checkable: the `updateLiveview()` predicate, the test count in `tests.md`, the
  route table in `app/app.md`. Filed as a candidate, not a commitment; most of what
  these docs say is reasoning, which no test can pin.

### 47. ⚪ Nits

- `camera/sony.py:8` — two dict entries jammed onto one line
  (`"focus_mode_widget": "focusmode","af_modes": …`).
- `camera/gp2.py:96-106` — `_bus` reads `self._busy_with` without the lock when
  building the `CameraBusy` message, and `close()` reads it again after the
  raise. Benign, but the refusal can name `None` or a stale operation, which
  undercuts the "the refusal names the holder is most of the diagnosis" claim in
  `camera.md`.

---

## Suggested order

**Done:** #1, #5, #6, #37, #45, #46 (all verified on hardware except #45/#46,
which are documentation-only).
#2, #3, #4, #7, #8 have fixes applied and unit coverage but are 🧪. #2/#3/#4 are
dial-blocked, see below; #7 needs a body that is actually noisy on the event
stream; #8 needs a `systemctl`/`kill -STOP` session on the Pi (it also needs
`tools/setup.sh` re-run, or the unit edited by hand, to pick up `WatchdogSec`).

**Blocked on physical access to the camera's mode dial** (needs M + BULB, and
Long Exposure NR on): verifying #2, #3, and #38. Nothing else depends on this,
so it is not on the critical path — do the items below while it waits.

1. **#39, #40** — do these *with* the #8 hardware verification session, not
   after it. Both are defects in the watchdog itself, and #39 is the one failure
   mode the `kill -STOP` test cannot reveal (it is armed by then). Cheap: an
   ordering change and a `try/except`.
2. **#38** — refuse `bulb` when the body is not in BULB (the discovery command is
   in that item; it needs the dial too, but only to *read* one value)
3. **#13, #14** — quirk layering and vendor contract, **before** adding Canon/Nikon
4. **#17** — mostly closed from the config dump; what's left needs the rig:
   `af_area_size` corner-taps and the `autofocus`/`bulb` idle value
   (batch this with the #2/#3/#38 dial session — same setup, one trip)
5. **#9** — compare-and-swap in `_drop_camera`
6. **#41** — batch with #30 and #32; all three are the same lifespan/task-hygiene
   pass, and #40 is the fourth
7. **#10** — single shared liveview producer (largest change; what makes multi-client work)
8. Everything else, opportunistically. #42-#44 and #47 are all small and
   independent — good filler while hardware access is blocked.

#13 and #34 each have an `@expectedFailure` acceptance test already written in
`tests/test_known_gaps.py` — start there. #5's and #6's have been promoted into
the main suite now that they pass.
