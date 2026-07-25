# Pathfinder — Outstanding Issues

Findings from a full-codebase review (2026-07-25). Ordered by suggested fix order:
physical-hardware risk first, then correctness, then structure, then hardening.

Each item states **what** is wrong, **why** it matters, and a suggested fix.
Line numbers refer to the state of the tree at commit `da23621`.

**Legend:** 🔴 critical · 🟠 high · 🟡 medium · ⚪ low / nit

---

## Tier 1 — Fix first (small, localized, physical-hardware risk)

### 1. 🔴 Bulb exposure length is unvalidated — can lock the device with the shutter open

- **Where:** `app.py:28-29` (`BulbExposure`), `camera/gp2.py:101-121`
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

### 2. 🔴 Bulb shutter release is single-shot — a failed write leaves the shutter open

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

### 3. 🔴 Bulb readout timeout is fixed at 15s — long exposures always report failure

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

### 4. 🟠 AF drive has no fail-safe release (same shape as #2)

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

### 5. 🟠 No server-side clamping of RANGE widgets — `/api/focus` can drive the lens past its stops

- **Where:** `app.py:24-25` (`FocusStep`), `camera/gp2.py:189-195`,
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

### 6. 🟠 `POST /api/settings/{name}` can write *any* widget in the tree

- **Where:** `app.py:271-281` → `camera/gp2.py:235-241`
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

### 7. 🟠 `_drain_events` can spin forever holding the lock

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

### 8. 🟠 No operation can ever time out, and there is no watchdog

- **Where:** whole stack — `camera/gp2.py` (all `self._cam.*` calls),
  `app.py:110-117` (`_run_camera`), `setup.sh:161` (`Restart=on-failure`)
- **Issue:** `cam.init()`, `capture()`, `file_get()`, `set_config()` are blocking C
  calls into USB with no bound. If the camera wedges mid-PTP transaction — which
  Sony bodies demonstrably do, per the existing `-52` self-healing — the
  threadpool worker is gone permanently. `run_in_threadpool` uses AnyIO's default
  limiter of **40 workers**; each hang burns one until the process is dead. A
  wedged `_try_connect` (`app.py:58`) is worse: it also holds `_connect_lock`, so
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

- **Where:** `app.py:99-107`
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

- **Where:** `app.py:167-195`, `web/script.js:28-43`
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
    the generic handler at `app.py:180`, which sleeps 0.3s and **retries forever**
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

- **Where:** `camera/sony.py:12-14`; `camera/camera.md:224` already flags AF-point
  as best-guess
- **Issue:** Raising the confidence level on *why* those guesses look wrong:
  - **`af_area_widget: "changeafarea"` is a Canon EOS widget name.** libgphoto2's
    PTP driver exposes `changeafarea` for Canon; Sony bodies generally don't have
    it. `af_area_size: (640, 480)` is likewise Canon's liveview coordinate space.
    The tap-to-focus feature added in `da23621` is quite likely a no-op on the
    actual target body.
  - **`bulb_widget: "bulb"` is also mostly a Canon convention.** Note the guard at
    `gp2.py:107` checks `if not widget` — which *passes*, because the quirk is set
    — so an absent widget produces a raw libgphoto2 error instead of the clean
    "bulb is not supported on this body" message.
  - **The `autofocus` toggle idles at `2`,** per the hardware notes, but
    `af_drive_values` restores it to `0`. Probably benign; the widget is left in a
    state the body doesn't consider idle.
- **Why it matters:** Two shipped features may not work at all on the only
  field-tested camera, and both fail with cryptic 400s rather than "unsupported."
- **Fix:** `gphoto2 --list-all-config | grep -i -E 'af|bulb'` on the rig; correct
  the table. Make the unsupported-widget path produce the friendly message by
  probing the widget's existence at connect time rather than trusting the quirk.

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

- **Where:** `app.py:271-281`
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

- **Where:** `setup.sh:12` (`AP_PASS="pathfinder"`), published in `README.md:24`
- **Issue:** A hardcoded, documented, 11-character PSK identical across all units.
- **Why it matters:** Anyone in WiFi range gets full camera control — including the
  arbitrary widget writes of #6. A published default credential is not a
  credential.
- **Fix:** Generate a random per-device PSK during provisioning and print it once
  at the end of `setup.sh`.

### 22. 🟡 No authentication, binds `0.0.0.0:8080`

- **Where:** `run.py:9`
- **Issue:** Fine behind the AP. Not fine in the `AP_ON_BOOT=0` development mode the
  README explicitly recommends (`README.md:87`), where the entire home LAN gets
  unauthenticated shutter control.
- **Why it matters:** The safe deployment and the documented dev workflow have
  different threat models, but identical code.
- **Fix:** At minimum bind to the AP interface only when the AP is up. A shared
  token in a query string or header would cover the dev case.

### 23. 🟡 CSRF on the body-less POST endpoints

- **Where:** `app.py:138` (`/api/capture`), `app.py:210-217` (`/api/record/*`)
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

- **Where:** `setup.sh:156-160`
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

- **Where:** `setup.sh:58` (library), `setup.sh:77` (CLI); `requirements.txt`
  (lower bounds only)
- **Issue:** Every device provisioned on a different day gets a different library
  build. Python deps have the same problem — `fastapi>=0.110` resolves to whatever
  is newest at provision time.
- **Why it matters:** When an upstream commit breaks Sony support, you'll have
  units in the field that work and units that don't, **with no way to tell them
  apart.** Reproducible provisioning is the difference between "we shipped a known
  build" and "we shipped whatever was on master that morning."
- **Fix:** Pin a tag or commit SHA for both repos. Pin exact versions in
  `requirements.txt` (or add a lockfile), and record the built library version
  somewhere queryable at runtime.

### 26. 🟡 `setup.sh` re-run hazards

- **Where:** `setup.sh:46`, `:56`, `:76`, `:96`
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

- **Where:** `log.py:4` (`DEFAULT_LEVEL = "DEBUG"`)
- **Issue:** On a Pi writing to persistent journald, with a 3-second reconnect poll
  (`app.py:51`) and per-frame liveview debug lines (`app.py:181`).
- **Why it matters:** Flash endurance is a hard constraint on this platform, not a
  theoretical one — this is a wear-out failure with a slow fuse.
- **Fix:** Default to `INFO`. Consider `Storage=volatile` in journald, or a
  size-capped persistent journal.

### 28. ⚪ `logs/` is a dead directory

- **Where:** `logs/debug.log`, `log.py:14`
- **Issue:** `configure_logging` only calls `basicConfig` (stderr → journald).
  Nothing writes `logs/debug.log`; it's a stale artifact of an earlier design.
- **Fix:** Delete it, or wire up a real file handler if one is actually wanted.

---

## Tier 6 — Smaller items

### 29. ⚪ `os.environ.setdefault("LD_LIBRARY_PATH", ...)` is a no-op

- **Where:** `app.py:13`
- **Issue:** glibc reads `LD_LIBRARY_PATH` at process start; setting it in-process
  does not affect later `dlopen` search paths. It works today only because of
  `setup.sh:100` (`ld.so.conf.d`) and the systemd `Environment=` line
  (`setup.sh:159`).
- **Why it matters:** It makes the `import camera` placement below it look
  load-bearing. Someone will "fix the lint" by hoisting the import and conclude
  nothing broke — which is true, but for the wrong reason.
- **Fix:** Remove the line; add a comment noting the real mechanism.

### 30. 🟡 Blocking USB I/O on the event loop during startup and shutdown

- **Where:** `app.py:71` (`_try_connect`), `app.py:80` (`set_recording`),
  `app.py:84` (`camera.disconnect`)
- **Issue:** All three run directly on the event loop, not via
  `run_in_threadpool`.
- **Why it matters:** Startup blocks the server on the USB handshake; shutdown can
  hang until systemd SIGKILLs at 90s, which skips the "stop recording" cleanup that
  block exists to perform.
- **Fix:** Wrap in `run_in_threadpool` with an `asyncio.wait_for` timeout.

### 31. ⚪ CWD-relative paths

- **Where:** `app.py:284` (`StaticFiles(directory="web")`), `camera/gp2.py:12`
  (`CAPTURE_DIR = "captures"`)
- **Issue:** Correct only because the systemd unit sets `WorkingDirectory`. Running
  `python run.py` from anywhere else crashes at import or writes captures to a
  surprising location.
- **Fix:** `Path(__file__).parent / "web"`, and resolve `CAPTURE_DIR` against the
  package root.

### 32. 🟡 A watcher crash silently ends all reconnection

- **Where:** `app.py:61-64`
- **Issue:** If `_camera_watcher`'s body ever raises, the task dies. Nothing logs
  it and nothing restarts it.
- **Why it matters:** The device would appear permanently "no camera connected"
  with no diagnostic — the reconnect feature would be gone with no trace.
- **Fix:** `try/except` inside the loop, log, and continue.

### 33. ⚪ Inconsistent error mapping across routes

- **Where:** `app.py:259-268` (`/api/telemetry`, `/api/settings`), `app.py:138-145`
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

### 35. ⚪ Settings panel re-renders mid-interaction

- **Where:** `web/script.js:284-294` (`applySetting`)
- **Issue:** Every change re-renders the whole panel, destroying the `<select>` or
  slider the user is currently touching. On mobile this closes the picker
  mid-interaction.
- **Fix:** Patch only the changed row, or skip re-render for the element that has
  focus.

### 36. ⚪ Unused dependency

- **Where:** `requirements.txt:3` (`websockets>=12`)
- **Issue:** No WebSocket code anywhere in the tree.
- **Fix:** Remove it.

### 37. 🟠 No tests

- **Where:** entire repo
- **Issue:** Nothing exercises quirk resolution, `_coerce` clamping, the
  error→HTTP-status mapping, or the disconnect/reconnect state machine.
- **Why it matters:** The project targets ~2,000 camera models with per-model quirk
  tables — the exact shape of problem where a regression is invisible until someone
  plugs in the one body you broke. Several items above (#13, #15, #34) are
  precisely the failures a test suite catches for free.
- **Fix:** A mocked `gphoto2` layer covering the pure logic (quirks, coercion,
  status mapping). libgphoto2 also ships a dummy/vusb driver that can be driven in
  CI without hardware.

---

## Suggested order

1. **#1, #2, #3** — bulb bounds, fail-safe release, scaled readout timeout
2. **#5** — clamp RANGE widgets in `_coerce` (protects the focus motor *and* every slider)
3. **#6** — validate the settings widget name
4. **#4, #7** — AF fail-safe, bound `_drain_events`
5. **#8** — bounded lock acquisition + `WatchdogSec` (turns hangs into restarts)
6. **#13, #14** — quirk layering and vendor contract, **before** adding Canon/Nikon
7. **#17** — verify `changeafarea` / `bulb` / AF idle value on the rig
8. **#9** — compare-and-swap in `_drop_camera`
9. **#10** — single shared liveview producer (largest change; what makes multi-client work)
10. Everything else, opportunistically
