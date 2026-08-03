# Pathfinder — Outstanding Issues

Findings from a full-codebase review (2026-07-25); a second pass on 2026-07-30
after the watchdog work landed (#39-#49, in "Later findings"); and a third pass
on **2026-08-03** measuring the tree against the Version 1 scope requirement
*"full action and setting control over compatible Sony cameras"*
(`documentation/pathfinder_v1.tex`) — #50-#57, in "Requirement gaps" at the end.
Ordered by suggested fix order: physical-hardware risk first, then correctness,
then structure, then hardening.

Each item states **what** is wrong, **why** it matters, and a suggested fix.
**All line numbers were re-anchored to `5e242fc` (the focus-magnifier commit) on
2026-08-03** and are current as of that tree.

**Legend:** 🔴 critical · 🟠 high · 🟡 medium · ⚪ low / nit
· ✅ fixed & verified · 🧪 fix applied, awaiting hardware verification

---

## Requirement coverage — "full action and setting control"

The Version 1 scope commits to full action and setting control over compatible
Sony bodies, and `documentation/pathfinder_v1.tex` sells two specific
consequences: *"Full exposure control from the phone"* (Feature 6) and a
telemetry strip reading *"battery, shots remaining, lens, and model"*
(Feature 7). Measured against `tests/fixtures/ilce_7m4.txt` — a real
`--list-all-config` dump of the α7 IV — here is what the app actually reaches
today:

| Section | Widgets | Writable | Exposed by Pathfinder | Gap |
|---|---|---|---|---|
| `/main/actions` | 8 | 8 | **7** — all but `opcode` | `opcode` excluded on purpose (raw PTP); see #54 |
| `/main/settings` | 2 | 2 | 2 | — |
| `/main/imgsettings` | 4 | 3 | 3 | 1 read-only widget is hidden rather than shown (#56) |
| `/main/capturesettings` | 22 | 15 | 15 | 7 read-only widgets hidden, **including aperture and shutter speed** (#50, #56) |
| `/main/status` | 7 | 0 | 7 (telemetry) | remaining-shots and lens are **not here** (#52) |
| `/main/other` | 346 | 156 | **0** | ~30 carry real labels, **14 with no named equivalent** (#51); ~13 more are the modern Sony remote-control actions (#55); the rest are raw aliases or `PTP Property 0xNNNN` |

**The honest summary:** the *action* half of the requirement is met on this body
(7 of 8, the eighth deliberately withheld) but only because `camera/sony.py`
names each widget by hand — nothing generalises (#54). The *setting* half is met
for the three named sections and not at all for `/main/other`, which is where
Creative Style, Picture Profile, the movie file formats, interval-REC and ~150
other writable properties live (#51).

Two claims in the v1 document are **not currently true of the code**, and both
are cheap to settle:

- **Aperture and shutter speed never appear in the settings panel.** In the only
  dump we have they are `Readonly: 1`, and `_settable_widgets` drops read-only
  widgets — so the two controls the document names explicitly are absent from
  the UI, silently. See #50; the likely cause is benign (the dump was taken in
  **P**) but it has never been checked, and #56 is why nobody noticed.
- **"Shots remaining" and "lens" are not obtainable from `/main/status`.**
  Remaining shots is `/main/other/d249`; no lens property appears anywhere in
  the dump. See #52.

A third, unrelated to the camera: **a provisioned device does not host its own
WiFi on boot** (`AP_ON_BOOT` defaults to `0`), which the document's Feature 1
and success criteria both assume. See #53.

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
- **Where:** `camera/gp2.py:139-163` (`bulb`), `camera/gp2.py:327-339`
  (`_release_action`)
- **Issue (pre-fix):**
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
- **Where:** `camera/gp2.py:172-179` (`_wait_for_image`), called at
  `camera/gp2.py:160`
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
  `camera/gp2.py:227-242` (`autofocus`)
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
- **Where:** `app/app.py:46-47` (`FocusStep`), `camera/gp2.py:244-250`
  (`manual_focus`), `camera/gp2.py:432-449` (`_coerce` / `_within_range`)
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
  `spotfocusarea`, `focusmagnifier` — are now unreachable through this endpoint.
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

- **Where:** `app/app.py:399-411` → `camera/gp2.py:346-352`, resolved through
  `camera/gp2.py:375-388` (`_settable_widgets` / `_settable_widget`)
- **Issue (pre-fix):** `list_settings()` carefully filtered to `INCLUDE_SECTIONS`
  and dropped read-only widgets. `set_setting()` re-checked **neither** — it
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
- **Where:** `camera/gp2.py:189-199`
- **Issue (pre-fix):**
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
- **Where:** whole stack — `camera/gp2.py:99-110` (`_bus`, wrapping every
  `self._cam.*` call), `app/app.py:211-220` (`_run_camera`),
  `app/app.py:138-155` (`_watchdog`), `tools/setup.sh:143-146`
  (`Restart=always` / `WatchdogSec=30` / `NotifyAccess=main`)
- **Issue:** `cam.init()`, `capture()`, `file_get()`, `set_config()` are blocking C
  calls into USB with no bound. If the camera wedges mid-PTP transaction — which
  Sony bodies demonstrably do, per the existing `-52` self-healing — the
  threadpool worker is gone permanently. `run_in_threadpool` uses AnyIO's default
  limiter of **40 workers**; each hang burns one until the process is dead. A
  wedged `_try_connect` (`app/app.py:63-74`) is worse: it also holds `_connect_lock`, so
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

- **Status:** open, unchanged as of `5e242fc`. `_drop_camera` still takes only
  the exception and `_run_camera` still does not forward the camera it used.
- **Where:** `app/app.py:200-208` (`_drop_camera`), called from
  `app/app.py:211-220` (`_run_camera`)
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

- **Where:** `app/app.py:268-305` (`/api/liveview`), `web/script.js:35-50`
- **Since the #8 fix:** the first two consequences below are bounded, not gone.
  `preview()` takes the bus with `PREVIEW_BUS_TIMEOUT` (0.25s) and a refused
  frame is a 409 the generator treats as "pause" (`app/app.py:284-289`), so a
  second tab no longer parks a worker for a whole bulb — it burns one 0.25s
  acquisition attempt every 0.3s instead. The third (a `RuntimeError` during
  recording retried forever at 3.3 Hz, `app/app.py:290-293`) is unchanged.
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

- **Where:** `camera/gp2.py:165-170` (`_download`)
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

- **Where:** `camera/gp2.py:214-225` (`set_recording`), `camera/gp2.py:93`
  (`self.recording = False` in `__init__`), `app/app.py:223-230` (`/api/status`
  publishes it)
- **Issue:** `set_recording` writes the widget and unconditionally sets the flag.
  If the body silently refuses (no card, wrong mode dial), the flag says
  "recording" and stills stay blocked until the user presses Stop. After a
  mid-record disconnect, `Gphoto2Camera.__init__` resets it to `False` while the
  camera is still rolling.
- **Why it matters:** State that models hardware without reading hardware back
  drifts, and the UI presents the drifted value as authoritative.
- **Fix:** Read the widget back after the write and derive the flag from the
  camera's reported value. On reconnect, query rather than assume.
- ✅ **The camera-truth read exists and is now identified** (2026-08-03, from
  the config dump): `/main/other/d21d` — **`Movie Recording State`**, `MENU`,
  `Readonly: 1`, choices `0/1/2`. That is the widget this item has always
  needed, and it costs nothing extra if it rides the shared config snapshot #49
  proposes. Two caveats before trusting it: `movie` (the *write* side) is
  `/main/actions/movie`, a different widget in a different section, so this is a
  genuine read-back rather than an echo; and the 0/1/2 encoding is unlabelled —
  the mapping to idle/recording needs one rig observation. Note also that
  reading it goes through `/main/other`, which `telemetry()` does not currently
  walk — so this lands with #52, not before it.

---

## Tier 3 — Structure & future camera support

### 13. 🟠 Vendor quirks replace the defaults instead of layering over them

- **Where:** `camera/gp2.py:452-461` (`_quirks_for`), `camera/gp2.py:26-43`
  (`DEFAULT_QUIRKS`), `camera/sony.py:1-18` (`GENERAL`)
- **Issue:** `_quirks_for` returns the vendor dict as-is, so `sony.GENERAL` has to
  re-specify all **16** keys — duplicating `DEFAULT_QUIRKS` in full. (It was 13
  when this was filed; the magnifier work added `magnifier_widget` and
  `magnifier_off`, and both had to be written twice. The duplication predicted
  here is now measurable: the table has grown by 3 keys and every one of them
  landed in two files.)
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

- **Where:** `camera/gp2.py:44` (`VENDORS = [sony]`)
- **Issue:** The backend duck-types a module-level `quirks(model)` function. What a
  vendor module owes the backend is documented in prose only.
- **Why it matters:** This is the first file a contributor adding Nikon reads. An
  explicit contract is both documentation and a test surface.
- **Fix:** A `Protocol` (or a small registry decorator) plus a validation pass over
  each registered vendor at import.

### 15. 🟡 Model matching is fragile, and the per-model layer has never been exercised

- **Where:** `camera/sony.py:20-33`
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

- **Where:** `camera/gp2.py:309-320` (`_ensure_focus_mode`), `camera/sony.py:8-12`
- **Issue:** Two problems:
  - It writes one literal target (`af_target_mode`, `"AF-A"`). If a body doesn't
    offer that exact string, **every AF press 400s permanently.**
  - Pressing AF silently mutates the camera's focus mode and never restores it. On
    Sony bodies focus mode also has physical switch interactions, so the driver may
    accept a write the hardware overrides.
  Also, `af_modes` lists `"AF-S"`, which the recorded a7 IV `focusmode` choices
  (`Automatic/AF-A/AF-C/DMF/Manual`) do not contain — harmless as an extra
  "acceptable" entry, but it signals the table is partly guessed. Confirmed
  against the dump 2026-08-03: `/main/capturesettings/focusmode` is a `RADIO`
  with exactly **5** choices and `Current: AF-A`, so `af_target_mode` is right
  on this body and `"AF-S"` is indeed dead weight.
- **Why it matters:** A hardcoded string is a single point of failure across ~2,000
  supported bodies, and the failure is total (the button never works) rather than
  degraded.
- **Fix:** Choose the first entry of `acceptable` that is actually present in the
  widget's `get_choice(...)` list, rather than trusting one literal. Document — or
  restore — the focus-mode mutation.
- **Recover the lost comment while you're here.** `7372a98` ("update sony.py
  concise code structure") deleted the block comment that explained what
  `af_modes`/`mf_modes` *mean* — that they list the modes in which the action
  actually reaches the motor, that the button leaves an already-acceptable mode
  alone, and that `DMF` was left out of `mf_modes` pending verification on the
  body. That reasoning now exists only in git history, and it is precisely the
  reasoning someone adding a second vendor needs. Restore it, or move it into
  `camera/camera.md` next to the quirk table.

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
    `if not widget` guard at `gp2.py:144-146` only checks the quirk is set, not
    that the widget exists, so a body without it still gets a raw libgphoto2
    error.)
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
  only moves when `focusarea` is one of the `Flexible Spot` choices — the dump
  shows `/main/capturesettings/focusarea` is a writable `RADIO` with 14 choices,
  currently `Wide`, so **tap-to-focus cannot work at all in the state the dump
  was taken in.** Set `focusarea` to a Flexible Spot choice *first*, or the
  corner-tap experiment measures nothing. That the app never checks this is
  itself a gap: `set_af_point` should refuse (409) rather than write into a
  focus mode that ignores it — same family as #38.
- **Second candidate for the same job, found 2026-08-03:**
  `/main/other/d2dc` — **`AF Area Position`**, `RANGE`, writable. It belongs to
  the newer Sony remote-control property family (#55) and is the more likely
  modern path for a coordinate write than a `TEXT` widget that takes a
  comma-joined string. It advertises `Bottom: 0 Top: 0 Step: 0` in the dump,
  i.e. no usable range while idle — which is exactly why a dump can't settle
  this and the rig must. Try both in the same session; if `d2dc` works, the
  `af_area_size` question dissolves (the widget carries its own range) and
  `_within_range` clamping already covers it.

### 18. 🟡 Whole-tree config writes where single-widget writes belong

- **Where:** `camera/gp2.py:346-352` (`set_setting`), `gp2.py:309-320`
  (`_ensure_focus_mode`), `gp2.py:214-225` (`set_recording`) — versus
  `gp2.py:322-325` (`_drive_action`), which does it correctly
- **Issue:** These read the entire config tree and write it back, rather than using
  `get_single_config` / `set_single_config`.
- **Why it matters:** Whole-config writes are slow on Sony (hundreds of ms to
  seconds) and are the classic source of "the camera changed a setting I didn't
  touch." The codebase already has the right pattern in `_drive_action` — this is
  an internal inconsistency, not an unknown.
- **Fix:** Move all three to the single-config path.
- ⚠️ **Read #48 before starting this.** The magnifier bug established that on
  Sony a single-widget *read* serves a cached property store a write does not
  invalidate. So this item is only safe for the **write** half: `set_setting`'s
  write can move, but the `list_settings()` re-read that follows it must stay on
  `get_config()`, or the settings panel starts showing pre-write values the way
  the magnifier select did. `_ensure_focus_mode` reads *then* writes, which is
  the same hazard in the other order — it would decide against a stale mode.

### 19. ⚪ Every setting change costs three full config reads

- **Where:** `app/app.py:399-411` (`set_setting` route), `camera/gp2.py:346-352`
  + `camera/gp2.py:341-344`
- **Issue:** `set_setting` does a full `get_config`, then the handler returns
  `cam.list_settings()` which does another.
- **Why it matters:** Combined with the 400ms debounce and range `change` events,
  the UI will feel laggy and will hog the USB bus that liveview and capture need.
- **Fix:** Return only the changed widget's descriptor, or cache the config tree.

### 20. 🟠 A camera in MTP/Mass Storage mode connects "successfully" but does nothing

- **Where:** `camera/gp2.py:464-472` (`connect`)
- **Issue:** If a Sony body is in MTP/Mass Storage rather than PC Remote,
  `init()` **succeeds** but the config tree is nearly empty.
- **Now cheap to detect, and cheap to phrase:** `_settable_widgets` already
  yields exactly the writable surface, and the α7 IV's healthy count is a
  measured 20 (see the coverage table at the top). A `connect()` that walks it
  once and refuses — or warns — on an empty result costs one config read on a
  path that already does several. #56 makes the same failure visible in the UI
  from the other direction: a panel showing read-only rows would be visibly
  *empty* here rather than ambiguously empty.
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

- **Where:** `app/app.py:243-250` (`/api/capture`), `app/app.py:320-327`
  (`/api/record/*`)
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

- **Where:** `tools/setup.sh:137-146` (the generated unit's `[Service]` block)
- **Issue:** `User=$USER` (`:137`) — typically a member of `sudo`. No hardening
  directives.
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

- **Where:** `tools/setup.sh:4` + `:39` (library repo and clone),
  `tools/setup.sh:6` + `:58` (CLI); `tools/requirements.txt` (lower bounds only)
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

- **Where:** `tools/setup.sh:28`, `:37`, `:56`, `:78`
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
  (`app/app.py:77`, `CAMERA_POLL_INTERVAL`) and per-frame liveview debug lines
  (`app/app.py:287`, `:291`).
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

- **Where:** `app/app.py:14`
- **Issue:** glibc reads `LD_LIBRARY_PATH` at process start; setting it in-process
  does not affect later `dlopen` search paths. It works today only because of
  `tools/setup.sh:82-83` (`ld.so.conf.d` + `ldconfig`) and the systemd
  `Environment=` line (`tools/setup.sh:141`).
- **Why it matters:** It makes the `import camera` placement below it look
  load-bearing. Someone will "fix the lint" by hoisting the import and conclude
  nothing broke — which is true, but for the wrong reason.
- **Fix:** Remove the line; add a comment noting the real mechanism.

### 30. 🟡 Blocking USB I/O on the event loop during startup and shutdown

- **Where:** `app/app.py:162` (`_try_connect` in `lifespan`), `app/app.py:181`
  (`set_recording`), `app/app.py:185` (`camera.disconnect`)
- **Issue:** All three run directly on the event loop, not via
  `run_in_threadpool`.
- **Why it matters:** Startup blocks the server on the USB handshake; shutdown can
  hang until systemd SIGKILLs at 90s, which skips the "stop recording" cleanup that
  block exists to perform.
- **Fix:** Wrap in `run_in_threadpool` with an `asyncio.wait_for` timeout.

### 31. ⚪ CWD-relative paths

- **Where:** `app/app.py:414` (`StaticFiles(directory="web")`), `camera/gp2.py:14`
  (`CAPTURE_DIR`)
- **Issue:** Correct only because the systemd unit sets `WorkingDirectory`
  (`tools/setup.sh:140`). Running `python tools/run.py` from anywhere else
  crashes at import or writes captures to a surprising location. `CAPTURE_DIR`
  gained a `PATHFINDER_CAPTURE_DIR` override since this was filed, but its
  **default** is still the CWD-relative `"captures"`, so the hazard is unchanged
  for anyone who doesn't set it.
- **Fix:** `Path(__file__).parent / "web"`, and resolve `CAPTURE_DIR` against the
  package root.

### 32. 🟡 A watcher crash silently ends all reconnection

- **Where:** `app/app.py:97-100`
- **Issue:** If `_camera_watcher`'s body ever raises, the task dies. Nothing logs
  it and nothing restarts it.
- **Why it matters:** The device would appear permanently "no camera connected"
  with no diagnostic — the reconnect feature would be gone with no trace.
- **Fix:** `try/except` inside the loop, log, and continue.

### 33. ⚪ Inconsistent error mapping across routes

- **Where:** `app/app.py:369-372` (`GET /api/magnifier`), `app/app.py:387-390`
  (`/api/telemetry`), `app/app.py:393-396` (`GET /api/settings`),
  `app/app.py:243-250` (`/api/capture`), `app/app.py:411` (see #44)
- **Issue:** Three read routes have no generic handler, so gphoto2 errors become
  **500 + traceback**, while every sibling route maps them to 400.
  `/api/capture` handles `RuntimeError` (409) but lacks the `except Exception`
  that `/api/bulb` has, so an ordinary capture failure is also a 500.
- **Grown since filing:** `GET /api/magnifier` was added by the magnifier work
  and inherited the same omission — which is the argument for the fix below.
  Every new read route so far has repeated this by default, because the correct
  behaviour lives in the routes that happen to have remembered it rather than in
  one place. Four routes now share a bug that no route would have if the mapping
  were a decorator.
- **Fix:** Factor the shared error mapping into one decorator or helper so every
  route behaves the same way, and so a *new* route gets it by construction.

### 34. ⚪ `_capture_with_retry` can return `None`

- **Where:** `camera/gp2.py:201-212`
- **Issue:** If a quirk sets `capture_retry_attempts <= 0`, the loop body never
  runs and the function falls off the end returning `None` →
  `AttributeError: 'NoneType' object has no attribute 'name'` in `_download`.
- **Why it matters:** A plausible mistake in a future vendor file, surfacing as a
  confusing crash far from its cause.
- **The same shape now appears twice more**, both introduced after this was
  filed, both from module constants rather than quirks (so not reachable today,
  but the pattern has spread and should be fixed once, together):
  - `camera/gp2.py:327-339` (`_release_action`) — with `RELEASE_ATTEMPTS <= 0`
    the loop never binds `failure`, and the trailing `raise failure` is an
    `UnboundLocalError`.
  - `camera/gp2.py:285-295` (`_settled_magnifier`) — with
    `MAGNIFIER_SETTLE_ATTEMPTS <= 0` the trailing `log.warning(...)` and
    `return state` reference an unbound `state`.
  Each is a bounded loop whose "ran zero times" branch was never written. The
  general fix is the same in all three: clamp the count at the point of use
  (`max(1, n)`), or assert it at import.
- **Fix:** Validate the quirk value (`max(1, attempts)`) or raise explicitly.
- **Test:** acceptance test waiting at
  `tests/test_known_gaps.py::RetryBounds` (expected-failure).

### 35. ⚪ Settings panel re-renders mid-interaction

- **Where:** `web/script.js:329-339` (`applySetting`), rendering through
  `web/script.js:307-319` (`renderSettings`)
- **Issue:** Every change re-renders the whole panel, destroying the `<select>` or
  slider the user is currently touching. On mobile this closes the picker
  mid-interaction.
- **Worse than it reads:** `/main/capturesettings/capturemode` on the α7 IV has
  **139 choices** (drive modes, self-timer, every bracketing permutation). That
  is a 139-option `<select>` on a phone, rebuilt from scratch on every unrelated
  setting change — and it is also the argument for #51 shipping with grouping
  rather than one flat list.
- **Fix:** Patch only the changed row, or skip re-render for the element that has
  focus.

### 36. ⚪ Unused dependency

- **Where:** `tools/requirements.txt:3` (`websockets>=12`)
- **Issue:** No WebSocket code anywhere in the tree.
- **Fix:** Remove it.

### 37. ✅ FIXED — No tests

- **Status:** Fixed 2026-07-25. `tests/` — **339 tests** (228 at the time of the
  fix, 251 after #7, 295 before the hardware-fixture suite, 311 before the
  magnifier work), stdlib `unittest`, no third-party test dependencies. A fake
  `gphoto2` binding (`tests/fakes/`) is installed into `sys.modules` before
  `camera` is imported, so the whole camera layer runs with no libgphoto2 and no
  camera attached (**216 of 339** execute on the dev host, which has no pip).
  Covers exactly what this item asked for: quirk resolution, `_coerce`, the
  error→HTTP-status mapping, and the disconnect/reconnect state machine. See
  `tests/tests.md`.
- **Counts re-measured 2026-08-03 on the dev host:** `Ran 339 tests in 1.304s …
  OK (skipped=123, expected failures=2)`. `tests/tests.md:75` already carries
  339; this item said 311/197 and was the stale one. The two expected failures
  are still the remaining `test_known_gaps.py` items (#13, #34).
- **Last full run on the Pi, 2026-07-26** (under `.venv/bin/python`, after the
  #7 fix): `Ran 251 tests in 1.328s … OK (expected failures=2)` with **zero
  skips** — so the FastAPI/pydantic tests and the fake-vs-real-binding fidelity
  checks all executed against the genuine binding. **That run is now 88 tests
  out of date**; the magnifier and hardware-fixture suites have never been run
  against the real binding. Re-run it during the next Pi session — it is one
  command and it is the only thing that proves the fake and the binding still
  agree.
- **Note:** `tests/test_known_gaps.py` holds an `@expectedFailure` acceptance
  test for each remaining hazard — now #13 and #34, after #5's and #6's were
  promoted. Fixing one makes the suite red with an *unexpected success* — the
  cue to promote the test, not a regression.
- **Still open:** the libgphoto2 `vusb` dummy driver is unused, so nothing
  exercises the real binding end-to-end; there is no CI (the suite needs only
  `fastapi`+`pydantic`, so this is cheap); and the frontend has only static
  contract checks, no runtime tests.

### 38. 🟠 `bulb` reports success when the body is not in BULB mode

- **Where:** `camera/gp2.py:139-163` (`bulb`), `app/app.py:253-265` (`/api/bulb`)
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
- ⚠️ **The discovery command above will return nothing for `shutterspeed` as
  written** (found 2026-08-03). In the checked-in dump
  `/main/capturesettings/shutterspeed` is `Readonly: 1`, and `list_settings`
  drops read-only widgets — so `/api/settings` does not contain it at all, in
  any mode. Read it with `gphoto2 --get-config shutterspeed` with the service
  stopped, or land #56 first (which makes read-only widgets visible and turns
  this one-off into an ordinary UI observation). Same correction applies to
  `expprogram`'s neighbours: `expprogram` itself *is* writable and does appear.
- **Useful prior from the dump:** `expprogram` reads `P` in the checked-in
  fixture, confirming that dump — and therefore the void run under #3 — was
  taken with the dial in **P**. When you re-dump in M + BULB, capture the whole
  tree (`tools/camera-dump.sh`), not just the one value: it settles this item,
  the `f-number`/`shutterspeed` question in #50, and #17's `focusarea` state in
  a single trip.
- **See also:** #17 (rig-verify quirk values), #3 (blocked behind the same dial
  access), #50 (the same read-only widgets, from the requirement side), #56.

---

## Later findings (second review pass, 2026-07-30)

Found after the watchdog work in `d841aa4`. Numbered in discovery order rather
than slotted into the tiers above, so existing references stay valid — severity
is in the emoji, and the fix order is in "Suggested order" below. #39-#41 are all
consequences of the #8 fix that the docs written alongside it didn't account for.

### 39. 🟠 A wedged USB handshake at boot becomes a silent restart loop

- **Where:** `app/app.py:162` (`_try_connect` in `lifespan`), `app/app.py:163-171`
  (watchdog task creation), `tools/setup.sh:136` (`Type=simple`),
  `tools/setup.sh:143-146`
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

- **Where:** `app/app.py:138-155`
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

- **Where:** `app/app.py:178-187` (`lifespan` teardown)
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

- **Status:** still open — `.gitignore` covers `__pycache__/`, `.venv/`,
  `.vscode/` and `*.log`, but not `captures/` (re-checked 2026-08-03).
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

- **Where:** `app/app.py:411`
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

- ✅ `camera/sony.py:8` — the two dict entries jammed onto one line have been
  split; `GENERAL` is now one key per line. Fixed in `90755cf`.
- ⬜ `camera/gp2.py:99-110` — `_bus` reads `self._busy_with` without the lock when
  building the `CameraBusy` message, and `close()` (`:119-123`) reads it again
  after the raise. Benign, but the refusal can name `None` or a stale operation,
  which undercuts the "the refusal names the holder is most of the diagnosis"
  claim in `camera.md`.
- ⬜ `app/app.py:54-56` — `AfPoint.x/y` are bare `float`s with no `Field(ge=0,
  le=1)`. Harmless today because `_scale` (`camera/gp2.py:418-419`) clamps to
  `0..1` and `max(0.0, nan)` returns `0.0`, so even a `NaN` lands in-range — but
  that is an accident of `max`'s argument order, not a check. Every other model
  in the file bounds its inputs; this one is the exception.
- ⬜ `tools/requirements.txt:3` — `websockets>=12 ` has a trailing space and no
  explanatory comment, unlike every other line. Deleting it (#36) resolves both.

### 48. ✅ FIXED — Focus magnifier: a single-widget read serves a stale property cache

- **Status:** Feature added 2026-07-30; **fixed and verified on hardware
  2026-07-31** (α7 IV, firmware 4.00) via `tools/hardware-check.py`. The
  remaining work is upstream, not here.
- **Where:** `camera/gp2.py` (`_read_magnifier`, `set_magnifier`),
  `camera/sony.py` (`magnifier_widget`, `magnifier_off`)
- ✅ **The write format was fine.** The item opened worrying that
  `/main/actions/focusmagnifier` — a `RADIO` whose *get* returns
  `Off,332,249` — might need the full `level,x,y` triple its read side produces,
  the way `spotfocusarea` does. It does not: `check_magnifier` sets all four
  levels (`Off / 1 / 5.5 / 11`) with the bare choice label and reads each back.
- ✅ **The real defect was the read, and it is now measured rather than
  inferred.** The control displayed every change one selection behind (click
  `1×` → shows `Off`; click `11×` → shows `1×`). Cause: **`get_single_config`
  serves a Sony property cache that a write does not invalidate.**
  `check_read_paths` writes a level and samples both read paths in one bus hold:

  ```
  note  a single-widget read sees a write the tree read sees
        — no: single='Off' tree='1'
  ```

  Two reads of the same property, microseconds apart, disagreeing. `set_setting`
  never showed this because it re-reads through the whole-tree `get_config()`,
  which does re-read the property store from the body — and `telemetry()`'s 15s
  `get_config()` poll is what was refilling the cache, giving the staleness its
  tidy one-step look. `_read_magnifier` now reads
  `get_config().get_child_by_name(name)`.
- ⚠️ **The settle poll was removed and then restored — the removal was measured
  against conditions the removal itself changed.** It looked safe: across every
  level the loop never once iterated. But that run was taken while
  `set_magnifier` still did a whole-tree read *before* the write (to validate the
  level), and dropping that read was the other half of the same change. With it
  gone, the next hardware run read one level stale on **two of three** real
  transitions. `_settled_magnifier` is back as an attempts-based retry
  (`MAGNIFIER_SETTLE_ATTEMPTS` = 3, `MAGNIFIER_SETTLE_DELAY` = 0.1 s), matching
  `_release_action`'s shape. **Lesson: two edits justified by one measurement are
  not independent** — re-measure after each. See #49.
- 🟠 **The finding generalises, and it is this item's real legacy.** Single-widget
  reads are unreliable for **any** read-after-write on Sony. Nothing else in the
  tree reads an action widget's value back, so nothing else is affected today —
  but this bounds **#18** ("whole-tree config writes where single-widget writes
  belong"): the *write* half may be safe to move, the *read* half is not.
- ⬜ **Worth filing upstream.** A `libgphoto2` issue against the PTP/Sony driver:
  `camera_get_single_config` does not refresh the Sony property store the way
  `camera_get_config` does, so a read-after-write through the single-widget path
  returns a stale value with no error. This is a footgun for every Sony
  integrator, not just us, and `check_read_paths` is a ready-made reproduction.
- **Two lessons worth keeping.**
  1. *A hardware check that touches the state it measures is worse than no
     check.* The first `check_read_paths` drove its write through
     `cam.set_magnifier()`, which ends in a `get_config()` — so the tree read
     refreshed the property store before the single-widget read was sampled, and
     it reported a **clean pass against a body that is demonstrably stale**. That
     false negative would have justified reverting a working fix.
  2. *A red run must never be the correct outcome.* "The single read is stale" is
     a finding about libgphoto2, not a defect in our code, so it is reported as a
     `note` that leaves the exit code alone. Only claims we control are
     assertions.

### 49. 🟡 A magnifier change still holds the bus for one whole-tree read (~420 ms)

- **Status:** Halved 2026-07-31 (807 ms → ~420 ms) by removing the second tree
  read. What remains is structural and is the same cost as #19; open.
- **Where:** `camera/gp2.py` — `set_magnifier`, `_read_magnifier`
- **Measured on the α7 IV 2026-07-31** by `tools/hardware-check.py`: one
  whole-tree `get_config()` costs **416 ms**, and a level change cost **807 ms =
  1.9 tree reads**. That ratio was the whole diagnosis — two reads by design, so
  the settle poll had never once iterated and the entire cost was the reads.
- ✅ **Fixed: validation no longer costs a tree read.** `set_magnifier` used
  `_read_magnifier()` (whole-tree) purely to get the widget's choices to
  validate against. The staleness in #48 affects a widget's **value**, not its
  **choice list** — the check confirmed both paths publish identical
  enumerations — so validation moved to `get_single_config`. One tree read per
  settled change (~410 ms), down from two (807 ms).
- ⚠️ **Removing the settle poll alongside it was a mistake, now reverted.** See
  #48: the poll's measured redundancy was an artefact of the validating read that
  was removed in the same change. Re-measured green 2026-07-31: **295 ms settled,
  868 ms when a retry fires, and 2 of 4 changes needed one.** The typical change
  more than halved; the worst case is marginally above the 807 ms it replaced.
- ✅ **The cost is now defended, not just observed.** `check_magnifier` asserts
  the **best** change stays under `TREE_READ_BUDGET` (1.5) tree reads — a ratio,
  so the bound holds on any body regardless of USB speed — and reports the worst
  plus how many changes needed a settle retry as notes. Asserting the best rather
  than the worst is deliberate: a retry is legitimate behaviour, a validating
  tree read is not, and only the latter shows up in the floor.
  `test_gp2_camera.py::test_a_settled_change_costs_exactly_one_tree_read` counts
  `get_config` calls against the fake for the same property.
- **The retry rate is the new argument for the redesign below.** 2 of 4 changes
  lag on this body — routine, not exceptional — so the read-back is not merely
  expensive, it is often *doubled*. An optimistic write reconciled on a shared
  snapshot avoids the read and the retry together; nothing local can.
- ⬜ **The remaining ~410 ms needs a redesign, not tuning.** It is the read-back,
  and `get_config()` is the only fresh read this driver offers. It still exceeds
  `PREVIEW_BUS_TIMEOUT` (0.25 s), so liveview drops ~12 frames per settled
  change and ~26 when a retry fires, on a
  control used *while composing*. Getting to zero means not doing a dedicated
  read at all:
  1. **Write optimistically.** The write itself is single-config and cheap.
     Return the requested level, release the bus.
  2. **Reconcile on a shared snapshot.** `telemetry()` (every 15 s),
     `list_settings()`, and `_read_magnifier()` each do their own `get_config()`
     of the *same tree*. One periodic snapshot in the camera layer, with all
     three derived from it, makes reconciliation free — it rides a read that was
     already happening.
  This subsumes **#19** (three full config reads per setting change) and reduces
  liveview stalls across every write path, not just this one. `set_setting` has
  the identical cost today and no visible tell, which is the only reason it has
  never been reported.
- ⚠️ **Do not let the snapshot become another stale cache.** #48 was libgphoto2
  serving stale values; a snapshot is our own cache one layer up with the same
  failure mode. Rule to hold: the snapshot serves *periodic* reads (telemetry,
  settings panel, reconcile); anything that must be current immediately after a
  write reads fresh. Assert that split in `tools/hardware-check.py` so it is
  enforced rather than remembered.

---

## Requirement gaps (third review pass, 2026-08-03)

Measured against the Version 1 scope line *"full action and setting control over
compatible Sony cameras"* and the two capability claims in
`documentation/pathfinder_v1.tex`. Evidence throughout is
`tests/fixtures/ilce_7m4.txt`, the checked-in `--list-all-config` dump of the
α7 IV; the coverage table at the top of this file is the summary.

**Read this first, because it reframes #50-#52:** the app's writable surface is
defined by two module constants — `INCLUDE_SECTIONS` and `STATUS_SECTIONS`
(`camera/gp2.py:54-55`). They were chosen when #6 closed the arbitrary-write
hole, and choosing them was correct. But they are now the *only* thing deciding
what "full control" means, they were never revisited against the requirement,
and three of the four items below are consequences of that one pair of sets.

### 50. 🟠 Aperture and shutter speed never appear in the settings panel

- **Where:** `camera/gp2.py:375-381` (`_settable_widgets`, the
  `not widget.get_readonly()` filter), `tests/fixtures/ilce_7m4.txt`
- **Issue:** In the checked-in dump, `/main/capturesettings/f-number`
  (`RADIO`, 17 choices, `f/3.5`) and `/main/capturesettings/shutterspeed`
  (`RADIO`, 56 choices, `1/30`) are both **`Readonly: 1`**. `_settable_widgets`
  drops read-only widgets, so neither is in `/api/settings`, so neither is in
  the UI. The `/main/other` aliases are read-only too (`5007` F-Number, `d20d`
  Shutter speed), so there is no second path to them either.
- **Why it matters:** `documentation/pathfinder_v1.tex` Feature 6 promises
  *"Full exposure control from the phone"* and Example Use Case 4 has the
  operator adjusting *"ISO, aperture, and shutter speed from the phone"*. ISO
  and exposure compensation are genuinely there and writable. The other two
  named controls are not in the product at all, and nothing in the UI says so —
  they are simply missing rows. This is the single largest distance between the
  document and the code.
- **The likely explanation is benign, which is exactly why it needs checking.**
  The same dump has `/main/capturesettings/expprogram` reading **`P`**. In
  Program AE the body owns both aperture and shutter, so libgphoto2 reporting
  them read-only is *correct behaviour*, not a defect — and in **M** both would
  be expected to flip writable, making the claim true with no code change. But
  that has never been observed, the only dump we hold was taken in the one mode
  where it cannot be true, and the same P-mode dial position already produced
  one void verification round (#3). Do not write this off as "obviously fine";
  it is one dump away from being settled either way.
- **Fix, in order:**
  1. Re-dump in **M** (`tools/camera-dump.sh`) and compare the `Readonly` line
     on both widgets. Batch with #38/#3 — same dial trip.
  2. If they are writable in M: nothing to fix in the camera layer, but land
     #56 so a user in P can *see* why the controls are inert instead of finding
     the panel silently shorter.
  3. If they are still read-only in M: the write path is elsewhere (Sony's
     newer control properties, #55) and this becomes a real feature gap. Check
     `/main/settings/prioritymode` — it reads `Application` in the dump, which
     should already be the permissive setting, so rule it out rather than
     assume it.
- **Test:** add the assertion to `tests/test_fake_fidelity.py`, where the
  quirk-vs-hardware checks already live: for whatever the M-mode dump says,
  pin it, so a future libgphoto2 build that changes this is caught by the suite
  rather than by a photographer.

### 51. 🟠 `/main/other` is excluded entirely — 156 writable properties, ~30 of them real settings

- **Where:** `camera/gp2.py:54` (`INCLUDE_SECTIONS`), `camera/gp2.py:375-381`
- **Issue:** The α7 IV publishes **346** widgets under `/main/other`, **156 of
  them writable**, and Pathfinder reaches none of them. Most are noise — around
  200 carry the placeholder label `PTP Property 0xNNNN` — but roughly 30 carry
  real labels, and **14 of those are user-facing settings with no equivalent
  anywhere in the three included sections**:

  | Widget | Label | Widget | Label |
  |---|---|---|---|
  | `d23f` | Picture Profile | `d240` | Creative Style |
  | `d241` | File Format Movie | `d242` | Recording Setting Movie |
  | `d24f` | Interval REC Model | `d255` | AF Tracking Sens. Still |
  | `d25f` | Zoom Setting | `d26a` | Live View Image Quality |
  | `d262` | Wireless Flash | `d263` | Red Eye Reduction |
  | `d254` | Focus Magnifier Setting | `d223` | Date/Time Set |
  | `d210` | CC Filter | `d21c` | AB Filter |

- **Why it matters:** "Full setting control" cannot honestly mean the 20
  writable widgets in three sections while Picture Profile, both file formats
  and interval-REC sit unreachable. `d24f` (Interval REC) is also the hardware
  primitive the roadmap's intervalometer wants, and `d223` (Date/Time Set) is a
  free fix for the no-RTC clock problem in #11 — the *camera* has a real clock,
  and nothing currently reads or writes it.
- **The fix is emphatically not `INCLUDE_SECTIONS |= {"other"}`.** That
  re-opens #6 from the other side: it would expose ~200 unlabelled raw PTP
  properties, every read-only status property as an inert row, and — the part
  that actually breaks things — a pile of **duplicate controls**. At least a
  dozen `other` entries are raw aliases of named widgets, verified against the
  dump by matching choice counts:

  | Raw | Named equivalent | Tell |
  |---|---|---|
  | `d252` Jpeg Quality | `capturesettings/jpegquality` | both 4 choices |
  | `d253` File Format Still | `capturesettings/imagequality` | both 3 choices |
  | `d203` Image size | `imgsettings/imagesize` | both 3 choices |
  | `5005` White Balance | `imgsettings/whitebalance` | — |
  | `d21e` ISO | `imgsettings/iso` | — |
  | `5013` Still Capture Mode | `capturesettings/capturemode` | — |
  | `d0d9` Image Stabilization | `capturesettings/imagestabilization` | — |
  | `d222`, `d211`, `d231`, `d201`, `500a`, `500b`, `500c`, `5010`, `d22c` | capture-target, aspect-ratio, LV-effect, DRO, focus-mode, metering, flash, exp-comp, focus-area | — |

  Two rows writing the same property is not merely untidy: the raw side reports
  integer codes where the named side reports labels (`d253` offers `1/2/3`;
  `imagequality` offers `RAW/RAW+JPEG/JPEG`), so the panel would show the same
  setting twice with different vocabularies, and they would disagree after every
  write through the stale-read path in #48. **This is the argument for an
  allowlist rather than a section flag**, and it is why the table above lists 14
  and not 30.
- **Fix:** a per-vendor **allowlist** of `other` properties, in `camera/sony.py`
  beside the quirks, each entry carrying the friendly label the driver doesn't
  supply — e.g. `EXTRA_SETTINGS = {"d23f": "Picture Profile", …}`.
  `_settable_widgets` grows a second pass over `other` filtered by that map.
  Three properties this buys cheaply: the allowlist is data, so it is testable
  against the fixture the way `test_fake_fidelity.TheQuirksMatchTheHardware`
  already tests quirk names; the label problem is solved at the same time; and
  the duplicate-shadowing question is answered explicitly per entry instead of
  by accident. Land it **after** #13/#14 — an allowlist is a vendor-table key,
  and adding a 17th key to a table that is already duplicated in full makes the
  duplication worse.
- **Also needs a UI answer:** 20 rows is a scrolling list; 50 is a wall. #35's
  139-choice `capturemode` select is the warning. Grouping (Exposure / Image /
  Movie / Focus) should land with this, not after it.

### 52. 🟠 Telemetry can't produce two of the four readouts the v1 document promises

- **Where:** `camera/gp2.py:55` (`STATUS_SECTIONS = {"status"}`),
  `camera/gp2.py:354-362` (`telemetry`), `web/script.js:82-106`
- **Issue:** `telemetry()` walks `/main/status` only, which on the α7 IV is
  exactly 7 widgets: `serialnumber`, `manufacturer`, `cameramodel`,
  `deviceversion`, `vendorextension`, `batterylevel`, `focusindication`.
  Feature 7 of `documentation/pathfinder_v1.tex` promises *"battery, shots
  remaining, lens, and model"*:
  - **battery** ✅ `/main/status/batterylevel`
  - **model** ✅ `/main/status/cameramodel`
  - **shots remaining** ❌ — it exists, but as `/main/other/d249` *(Media SLOT1
    Remaining Shots, `RANGE`, read-only; `d257` for slot 2)*, in the section
    telemetry doesn't walk.
  - **lens** ❌ — **no lens property appears anywhere in the dump.** Not in
    `status`, not in `other`, not in the property summary. On this body and
    this driver it may simply not be reportable.
- **Why it matters:** Two of four promised readouts, one of which may be
  unobtainable rather than merely unwired. The lens claim is the more important
  one to resolve, because if libgphoto2 can't report it, the honest fix is to
  the document, not the code — and it is better to find that now than in front
  of someone holding the document.
- **Worth adding while the section is open** (all read-only, all in `other`,
  all genuinely useful on an unattended field device): `d251` Device Overheat
  Status, `d248`/`d256` Media SLOT1/2 Status, `d24a` Media SLOT1 Shooting Time
  (the movie-length counterpart to remaining shots), `d21d` Movie Recording
  State (this is the camera-truth read #12 needs), `d221` Live View Status.
- **Fix:** the same allowlist mechanism as #51 but for read-only properties —
  `TELEMETRY_EXTRAS` in the vendor module, merged into `telemetry()`'s walk.
  Ship the two together; they are one mechanism with two filters. Then correct
  the v1 document's Feature 7 wording to whatever survives.

### 53. 🟠 A provisioned device does not host its own WiFi on boot

- **Where:** `tools/setup.sh:14` (`AP_ON_BOOT="${AP_ON_BOOT:-0}"`),
  `tools/setup.sh:170-179`, `tools/setup.sh:126`
  (`connection.autoconnect no`), `README.md:139`, `README.md:149`
- **Issue:** `AP_ON_BOOT` **defaults to `0`**, so the finalize step leaves
  `connection.autoconnect no` on the AP profile and prints "AP will NOT
  auto-start". A device provisioned exactly as the README instructs comes up
  after reboot with no Pathfinder network — it rejoins home WiFi, or nothing.
- **Why it matters:** Both documents assume the opposite. `README.md:139`
  presents `AP_ON_BOOT=0` as an opt-in development toggle ("create the AP
  profile but don't auto-start it on boot"), which reads as a departure from
  the default; `README.md:149` then says "After it finishes, reboot — the AP
  and app both come up automatically." `documentation/pathfinder_v1.tex`
  Feature 1 is *"The unit hosts a WiFi network named Pathfinder"* and the
  "no app install, no internet" success criterion is recorded as **Met**. The
  failure is silent, happens only after the operator has left, and looks like a
  broken device rather than a configuration default.
- **Fix:** default `AP_ON_BOOT=1` — the product's behaviour should be the
  script's default, and the development case is the one that deserves the
  explicit opt-out. Keep `AP_ON_BOOT=0` documented as that opt-out. If the
  default is instead kept deliberately (e.g. to avoid stranding a Pi mid-
  provisioning over SSH — a real concern, since bringing the AP up drops the
  session), then say so in **both** documents and remove the "come up
  automatically" line, because right now the code and the docs disagree and the
  docs are what someone provisions from.
- **Note:** this is the one requirement gap with no camera involvement, and the
  cheapest to close.

### 54. 🟡 Action coverage is hardcoded per body — there is no action discovery

- **Where:** `app/app.py:243-384` (one bespoke route per action),
  `camera/sony.py:1-18` (the widget names they resolve through)
- **Issue:** Every action is a hand-written route resolving a hand-written
  quirk key. On the α7 IV this covers **7 of the 8** `/main/actions` widgets —
  `autofocus`, `manualfocus`, `bulb`, `movie`, `focusmagnifier`,
  `spotfocusarea`, plus `capture` via `gp.GP_CAPTURE_IMAGE` — with only
  `opcode` withheld. So the action half of the requirement is *met on this
  body*. It is met by enumeration, though: a body whose action set differs gets
  whatever `sony.py` happens to name and no way to discover the rest, and
  `DEFAULT_QUIRKS` (`camera/gp2.py:26-43`) guesses generic names
  (`autofocusdrive`, `manualfocusdrive`) that no Sony body has.
- **Why it matters:** The requirement says "compatible Sony cameras", plural.
  Today "compatible" means "listed in `sony.py`", and the only listed model has
  an empty override dict (#15). The first second body will reveal how much of
  this generalises, and the answer is currently "the parts that happen to share
  widget names."
- **Deliberate and worth writing down so nobody "fixes" it:**
  `/main/actions/capture` exists as a `TOGGLE` and is **not** what `capture()`
  drives — it uses `gp.GP_CAPTURE_IMAGE`, which handles the download handshake.
  And `opcode` (`TEXT`, `0x1001,0xparam1,0xparam2`) is a raw PTP command
  channel: it is excluded on purpose, and #6 is the reason. Neither is an
  oversight.
- **Fix:** fold action capability into the vendor contract from #14 — let a
  vendor module *declare* which actions it supports, and have `/api/status` (or
  a new `/api/capabilities`) publish that list so the UI can hide buttons the
  body can't honour, instead of showing a button that 400s. That is also the
  honest fix for the magnifier's `hidden` special-case in `web/script.js:175-182`,
  which solves this problem once, for one feature.

### 55. ⚪ The modern Sony remote-control property family is unexploited

- **Where:** `tests/fixtures/ilce_7m4.txt` (`/main/other/d2c1`-`d2ea`)
- **Issue:** The dump carries a contiguous block of writable control properties
  that look like the α7 IV's current-generation remote surface, none of which
  Pathfinder touches: `d2c1` ShutterHalfRelease, `d2c2` ShutterRelease, `d2c7`
  RequestOneShooting, `d2c3` AELButton, `d2c4` AFLButton, `d2c9` FELButton,
  `d2d9` AWBLButton, `d2d1` Manual Focus Adjust, `d2dc` AF Area Position,
  `d2dd` Zoom Operation, `d2e9`/`d2ea` Save/Load Zoom and Focus Position.
- **Why it's worth a rig session rather than a shrug:** three open items may
  have their answers in here. `d2dc` is a candidate tap-to-focus write (#17,
  where `spotfocusarea` is an untested `TEXT` widget); `d2d1` is a candidate
  focus-nudge write; `d2e9`/`d2ea` would give the roadmap's automated
  focus-transition feature a hardware primitive instead of a software
  approximation. AE/AF lock are also genuinely missing controls.
- **Caveat:** `d2d1`, `d2dc` and `d2dd` all advertise `Bottom: 0 Top: 0
  Step: 0` — no usable range while the body is idle. That is why the dump
  cannot settle any of this and the rig must, and it is also a trap for
  `_within_range` (`camera/gp2.py:441-449`), which would clamp every write to
  `0` against those bounds. Test with the body live and, if the ranges stay
  degenerate, treat a `0..0` range as "unknown" rather than as a clamp.
- **Fix:** add probes to `tools/hardware-check.py` — it already has the right
  shape for exactly this (write, read back, report as a `note` rather than an
  assertion when the finding is about the driver rather than our code). No
  shutter needs to fire for most of these.

### 56. 🟡 Read-only widgets are hidden rather than shown disabled

- **Where:** `camera/gp2.py:375-381` (`_settable_widgets` drops them),
  `camera/gp2.py:391-403` (`_describe` has no `readonly` field),
  `web/script.js:307-319` (`renderSettings`)
- **Issue:** A widget the body reports read-only is filtered out of
  `/api/settings` entirely, so the UI cannot distinguish "this camera has no
  such control" from "this control exists and is currently not writable." On
  the α7 IV that hides 8 widgets, including aperture, shutter speed, focal
  position and zoom.
- **Why it matters:** This is the reason #50 went unnoticed for the life of the
  project — a missing row looks like nothing at all. It is also why #20
  (MTP mode) presents as a mystery: the panel is empty, and an empty panel and
  a body with no controls look identical. Showing a disabled row with its
  current value turns three separate invisible failures into one visible,
  self-explaining state, and it is a smaller change than any of them.
- **Fix:** add `"readonly": widget.get_readonly()` to `_describe`, yield
  read-only widgets from a separate generator (keep `_settable_widget` — the
  **write** allowlist from #6 — exactly as it is; this must not widen it), and
  render those rows with the control `disabled`. The write path stays
  unchanged, which is the whole point: the read surface and the write surface
  are different questions, and #6 conflated them for good reasons that no
  longer apply to the read side.
- **Test:** `tests/test_gp2_camera.py::SetSetting` already asserts the round
  trip "everything listed is writable" — that assertion must be **restated**,
  not deleted: everything listed *and not marked read-only* is writable, and a
  read-only name still 404s at `set_setting`. Getting that backwards silently
  re-opens #6.

### 57. ⚪ The document that defines the requirement is untracked

- **Where:** `documentation/pathfinder_v1.tex` (untracked in git as of
  `5e242fc`), `documentation/pathfinder_v1.tex:3`
- **Issue:** `documentation/` is the only untracked path in the tree. The file
  is the source of the Version 1 scope statement, the feature claims and the
  success-criteria table — i.e. the thing #50-#53 are measured against — and it
  exists on exactly one machine. Its own compile comment names a different
  filename than the file has (`pathfinder-v1-information-document.tex`).
- **Fix:** commit it, and add `*.aux`/`*.log`/`*.out`/`*.toc`/`*.pdf` under
  `documentation/` to `.gitignore` (note `*.log` is already ignored globally,
  which will silently cover the LaTeX log too). Fix the filename in the header
  comment.
- **While it's open,** three claims in it need edits once the items above land:
  Feature 6's "full exposure control" (#50), Feature 7's "shots remaining, lens"
  (#52), and the success-criteria row *"Setup for the operator — no app install,
  no internet — Met"*, which is currently contingent on #53.

---

## Suggested order

**Done:** #1, #5, #6, #37, #45, #46, #48 (all verified on hardware except
#45/#46, which are documentation-only). #48 leaves two follow-ups: **#49** (its
read-back still costs ~420 ms of held bus) and filing the `get_single_config`
staleness upstream with libgphoto2.
#2, #3, #4, #7, #8 have fixes applied and unit coverage but are 🧪. #2/#3/#4 are
dial-blocked, see below; #7 needs a body that is actually noisy on the event
stream; #8 needs a `systemctl`/`kill -STOP` session on the Pi (it also needs
`tools/setup.sh` re-run, or the unit edited by hand, to pick up `WatchdogSec`).

**Two tracks now, and they don't compete for the same time.** Everything from
the first two review passes is *reliability* work on features that exist.
#50-#57 are *requirement* work — the distance between the code and what Version
1 says it does. The reliability list is longer, but the requirement list is what
someone reading `documentation/pathfinder_v1.tex` will find first, and #53 in
particular is a shipped-device defect with a one-line fix.

**One rig trip settles an unusual amount.** The dial session below is currently
blocking #2, #3, #38 *and* #50, and the same physical setup answers #17 and #55.
Take a full `tools/camera-dump.sh` in **M + BULB** while you are there and check
it in beside the P-mode fixture — several open questions are "we only ever
dumped in P", which is also what made #3's verification round void.

**Blocked on physical access to the camera's mode dial** (needs M + BULB, and
Long Exposure NR on): verifying #2, #3, #38, and settling #50. Do the items
below while it waits.

0. **#53** — the AP doesn't come up on a provisioned device. One line, no
   hardware, and it is the difference between a device that works out of the box
   and one that appears dead. Do it first for the same reason you'd fix a power
   rail before debugging a peripheral.
1. **#49** — half done (the cheap half). What's left is the shared config
   snapshot, which subsumes **#19** and cuts a whole-tree read out of every
   write path. Worth scheduling deliberately rather than bolting on — and note
   #52 adds a second consumer of that snapshot, so the two are better designed
   together than sequentially.
2. **#39, #40** — do these *with* the #8 hardware verification session, not
   after it. Both are defects in the watchdog itself, and #39 is the one failure
   mode the `kill -STOP` test cannot reveal (it is armed by then). Cheap: an
   ordering change and a `try/except`.
3. **#56** — show read-only widgets disabled. Small, self-contained, no
   hardware, and it makes #50 and #20 visible instead of invisible. Landing it
   *before* the rig trip means the M-mode check in #50 can be done by looking at
   the phone rather than by diffing dumps.
4. **#38** — refuse `bulb` when the body is not in BULB. Note the discovery
   command in that item needs correcting (see its ⚠️ bullet) or #56 landing
   first.
5. **#13, #14** — quirk layering and vendor contract, **before** adding
   Canon/Nikon *and before #51/#52*, which both want to add vendor-table keys.
   Adding keys to a table that is duplicated in full is how the duplication got
   from 13 keys to 16.
6. **#51, #52** — the `/main/other` allowlists, settings and telemetry. One
   mechanism, two filters; ship together. This is the bulk of what "full setting
   control" is missing, and #52 also hands #12 its camera-truth read.
7. **#17** — mostly closed from the config dump; what's left needs the rig:
   `af_area_size` corner-taps (with `focusarea` set to a Flexible Spot first —
   see the correction in that item), the `autofocus`/`bulb` idle value, and the
   `d2dc` alternative from #55. Batch with the dial session. Natural checks to
   add to `tools/hardware-check.py` rather than do by hand; #48 is the worked
   example of why that pays off.
8. **#9** — compare-and-swap in `_drop_camera`
9. **#41** — batch with #30 and #32; all three are the same lifespan/task-hygiene
   pass, and #40 is the fourth
10. **#10** — single shared liveview producer (largest change; what makes
    multi-client work)
11. **#54, #55** — action discovery and the Sony control-property family. Both
    are really "what does the second body need", so they belong with the
    Canon/Nikon work rather than ahead of it.
12. Everything else, opportunistically. #42-#44, #47 and #57 are all small and
    independent — good filler while hardware access is blocked.

#13 and #34 each have an `@expectedFailure` acceptance test already written in
`tests/test_known_gaps.py` — start there. #5's and #6's have been promoted into
the main suite now that they pass. When picking up #56, re-read the test note in
that item before touching `test_gp2_camera.py::SetSetting`: the round-trip
assertion it changes is what keeps #6 closed.
