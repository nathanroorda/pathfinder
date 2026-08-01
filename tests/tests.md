# Testing

Pathfinder's test suite: 339 tests, `unittest` only, no third-party test
dependencies, and **no camera required**. The whole camera layer runs against a
fake libgphoto2, so the suite is a normal edit-run-edit loop on a laptop rather
than something you can only do at the rig.

The test files follow the same convention as the rest of the project: no
docstrings, and comments only where something isn't recoverable from the code.
Test names state the claim (`test_transport_error_returns_503_and_drops_the_camera`),
so a failure is readable without opening the file — and **the reasoning lives
here** rather than in the source. If you want to know *why* a test exists, this
is the document.

```
tests/
├── tests.md              this file
├── run_tests.py          convenience runner (works from any directory)
├── support.py            bootstrap: installs the fake gphoto2, fixes the cwd
├── fakes/
│   ├── fake_gphoto2.py   the binding: constants, GPhoto2Error, CameraFilePath
│   ├── fake_camera.py    in-memory device, widget tree, and app-layer double
│   └── dump.py           parses real gphoto2 dumps into FakeWidget trees
├── fixtures/
│   ├── fixtures.md       what the dumps are, and how to re-take one
│   └── ilce_7m4.txt      captured off the real α7 IV (390 widgets)
                          (see also ../tools/hardware-check.py — assertions
                           that need a body, not a fake)
├── test_sony_quirks.py     9   vendor/model quirk resolution
├── test_gp2_helpers.py    37   coercion, clamping, describe, disconnect codes
├── test_gp2_camera.py    127   Gphoto2Camera against a fake device
├── test_app_models.py     31   request validation + the bulb ceiling
├── test_app_routes.py     67   routes, error mapping, connection state machine
├── test_web_contract.py   12   web/ ↔ app/app.py seams (static text checks)
├── test_watchdog.py       23   sd_notify, the heartbeat, and the systemd unit
├── test_fake_fidelity.py  31   do the doubles still resemble the real thing
└── test_known_gaps.py      2   TODO.md hazards, as expected-to-fail tests
```

## Running it

From the repo root:

```bash
python3 -m unittest discover -t . -s tests      # everything
python3 -m unittest tests.test_gp2_camera -v    # one module
python3 -m unittest tests.test_gp2_camera.Bulb  # one class
python3 tests/run_tests.py -v                   # same, from any directory
python3 tests/run_tests.py test_gp2_camera      # bare arg = filename glob
python3 tests/run_tests.py -k Quirks            # -k = test name, any module
```

`run_tests.py` exists only to remove a footgun: `app/app.py` mounts
`StaticFiles(directory="web")` at import time, so anything that imports it must
run with the repo root as the working directory. The runner (and `support.py`)
handles that for you.

Two things about its arguments are worth knowing, because the obvious guess is
wrong. A **bare argument is a filename glob**, not a test name — `run_tests.py
Quirks` looks for `Quirks.py` and finds nothing. Use `-k` for names; it follows
unittest's convention of matching anywhere in the dotted id, so `-k Quirks`
catches both `EveryDumpedBodyMatchesItsQuirks` and `TheQuirksMatchTheHardware`.
And **a run that matches nothing exits 2**, not 0. `wasSuccessful()` is `True`
for an empty result, so a typo'd filter would otherwise print `NO TESTS RAN` and
report success — which it did, until it was fixed.

### On the dev host vs. on the Pi

The dev machine has no pip, no venv, and no `gphoto2`/`fastapi`, so **118 tests
skip there** — everything that needs FastAPI or pydantic — plus the 5 that
compare the fake against the real binding. The remaining 216 execute, including
the entire camera layer:

```
Ran 339 tests in 1.3s
OK (skipped=123, expected failures=2)
```

That count assumes a hardware dump is present in `fixtures/`. With none it is
`skipped=139` and still green — see "The doubles" below.

(The run used to take 0.2s. The extra second is `BusTimeout`, which waits out
real lock deadlines — see below.)

To run the whole suite, use the Pi's venv (created by `tools/setup.sh`), which
has the real dependencies:

```bash
cd ~/pathfinder && .venv/bin/python tests/run_tests.py
```

Doing this is safe while the service is running: no test touches USB, opens a
port, or calls `camera.connect()` for real. It is also the only place
`test_fake_fidelity.py`'s upstream-comparison tests actually execute, because
they need the genuine binding installed to compare against. A green run on the
dev host is a real signal, but only a full run on the Pi covers the request
layer.

Note that `tests/` is not deployed by anything automatic — it goes to the Pi via
the same SFTP sync as the rest of the tree.

## The fake gphoto2

`camera/gp2.py` imports `gphoto2` at module scope and reads constants off it at
import time (`_KIND`, `_DISCONNECT_CODES`), so without the C extension the camera
layer cannot even be imported. `tests/support.py` installs
`tests/fakes/fake_gphoto2.py` into `sys.modules["gphoto2"]` before anything
imports `camera`, which is what makes the suite portable.

Three properties are deliberate:

- **The fake is installed unconditionally**, even on the Pi where the real
  binding exists. A unit test run must never depend on what is plugged in.
- **`fake_gphoto2.Camera()` always raises.** Nothing can fall through a missing
  mock into a real USB handshake; a test that reaches it fails loudly instead of
  quietly talking to hardware.
- **Every constant is the real libgphoto2 value**, not an invented one, and
  `test_fake_fidelity.py` checks that against the genuine binding whenever one is
  installed. A fake with a wrong widget-type number would let tests pass on a
  mapping that is wrong on the device — the exact failure a fake is supposed to
  prevent.

## The doubles

**`FakeWidget` / `default_config()`** — a *synthetic* config tree, shaped like a
camera but not copied from one. Section placement matters: `INCLUDE_SECTIONS`
decides what `list_settings` exposes, `STATUS_SECTIONS` what `telemetry` does,
and `actions` must be excluded from both while still being reachable by
`get_child_by_name` (which searches the entire tree). The tree deliberately
includes a read-only widget, a `GP_WIDGET_BUTTON`, a nested section, a `RANGE`
setting, and a status widget that raises on `get_value` — each one exists to pin
a filtering rule, and the real α7 IV has **none of them** (its settable sections
are all `RADIO`/`MENU`). That is the point: this tree exercises widget *shapes*.

**`dump.fixtures()`** — the opposite double: real trees parsed from the captured
dumps in `fixtures/`, all 390 widgets for the α7 IV including the 347 raw PTP
codes in `other`. Use it for any question of the form *"does this name/type/
choice really exist on the body?"*

Fixtures are **self-describing** — `dump.model_of()` reads the model out of each
file's `Abilities for camera` header — so there is no registry and no loader per
camera. Capture with `./tools/camera-dump.sh` on the Pi, commit, done:
`EveryDumpedBodyMatchesItsQuirks` covers the new body on the next run, and a dump
no vendor module claims is reported as a skip naming the model. An empty
`fixtures/` is legitimate; the hardware-backed tests skip and the suite stays
green.

Keeping both is deliberate, and the split is **shapes vs. names**. A synthetic
tree cannot answer whether `spotfocusarea` exists; a real dump cannot exercise a
`BUTTON` this body never publishes. Getting that backwards is what caused the
`changeafarea` bug — `default_config()` grew a widget because `camera/sony.py`
named one, so the AF-point tests were comparing the quirk table to itself and
passed for months against a feature that could not work. `test_fake_fidelity.py`
now asserts every quirk widget name against the dump, which cannot agree with a
name the hardware never had.

**`FakeDevice`** — what `gp.Camera()` returns, i.e. the object `Gphoto2Camera`
wraps. It reproduces libgphoto2's *snapshot* config model: `get_config()` returns
a detached copy, edits to that copy are local, and nothing reaches the device
until `set_config()` pushes it back. That distinction is load-bearing —
"`_ensure_focus_mode` leaves an acceptable mode alone" is only a meaningful
assertion if a forgotten `set_config` would be observable.

**`FakeConnectedCamera`** — the app-layer double. `app/app.py` never sees a
`FakeDevice`; it holds whatever `camera.connect()` returned and only calls
`Gphoto2Camera`'s public surface. That surface is an undeclared duck-typed
interface, so `test_fake_fidelity.py` pins both ends of it: the real camera must
answer every call `app/app.py` makes, and so must the double.

Failure injection, which is most of what the tests are built on:

| Seam | Use |
|---|---|
| `capture_results`, `preview_frames`, `events` | queues; an `Exception` in a queue is raised rather than returned |
| `errors[method]` (on `FakeConnectedCamera`) | raise a given exception from one camera call |
| `hook(method, *args)` | called at the top of every device method — raise on the Nth call, or inspect state mid-operation |
| `support.FakeClock` | replaces the `time` module *inside* `camera.gp2`, making shot-gap and exposure-length assertions exact instead of wall-clock flaky |

`FakeClock` only moves when something moves it, so `wait_for_event` advances it
on every call — by the full `timeout_ms` when the queue is empty, by
`event_poll_cost` when an event returns early. Both branches have to cost
something: the drain and readout loops are `while time.monotonic() < deadline`,
so a free poll is an infinite loop in the test rather than a failing assertion.
That fragility is why `test_the_bound_holds_on_the_real_clock` exists — it hands
the drain the genuine `time` module and a 50 ms timeout, so if the fake ever
stops paying for polls, one test still *fails* instead of every drain test
hanging. Cheap insurance against the one failure mode a suite can't report.

The `hook` is how the lock-holding tests work: they check the mutex from inside a
driven write, which is the only place that question can honestly be asked. It is
also how `Bulb` delivers its `GP_EVENT_FILE_ADDED` on the shutter's release edge
— queueing it earlier would just have it eaten by the pre-exposure drain, which
is what that drain is for.

## What the tests are actually checking

The suite is weighted toward the failures that hurt on this project — ones that
are invisible until a specific body is plugged into a device with no screen.

**The hardware stays in a safe state.** Every path out of `bulb()` ends with the
shutter closed, including an interrupted exposure and a readout timeout. The
mutex is verified *held* across the exposure — that is why an unbounded `seconds`
was a device lockup rather than a long wait. Every operation refuses a closed
connection instead of driving a dead handle. Shutdown stops recording before it
disconnects, so a service restart can't leave the body filling the card.

**Release edges get more than one chance.** The writes that return the shutter
and the AF toggle to rest are the ones that latch hardware if they never land, so
they retry (`RELEASE_ATTEMPTS`) and, if they still fail, raise the transport error
so the app drops and rebuilds the connection rather than reporting success. Two
properties are pinned separately: a *transient* failure is retried and the widget
ends at rest, and a *permanent* one surfaces loudly — with an ERROR log naming the
latched widget — instead of being swallowed. Note the second is the limit of what
software can do: if the bus is gone, no retry closes the shutter. A failure during
the exposure itself keeps its own exception rather than being masked by the
release's.

**The readout window scales with the exposure.** Long-exposure NR shoots a
matching dark frame, so a 60s bulb can take another ~60s before the file appears;
a fixed timeout would report failure on every long exposure while the frame
quietly landed on the card. The deadline is `seconds + BULB_READOUT_MARGIN`, and
tests pin both that it scales and that it still terminates rather than spinning on
the bus holding the mutex.

**Every wait on the body has a deadline.** `_drain_events` used to loop until the
camera volunteered a `GP_EVENT_TIMEOUT`, which a Sony streaming
property-change events never does — with `_lock` held, so the hang took every
other request with it. The tests pin the bound rather than the loop: a body whose
queue never empties (the `endless_events` hook re-arms an event on every poll)
still returns inside `DRAIN_TIMEOUT`, says so at `WARNING`, and leaves the mutex
free, while a body that *does* go quiet stops at the first quiet poll and is not
warned about. The failure this replaces is why `FakeDevice.wait_for_event`
charges clock time in both branches: a fake where polling is free would let a
deadline loop spin forever in-process, and the test would hang instead of fail.
A transport error during a drain is asserted to be *logged and swallowed* — the
capture that follows is where a dead bus gets attributed to a request, and it is
still checked to reach the caller from there.

**Nothing waits on the bus forever, and the process says so when it is stuck.**
This is TODO #8, and it has three seams. (1) `BusTimeout` occupies the camera
lock from a second thread — through `capture()`, so the contention is the real
thing rather than a poked mutex — and pins that every other operation is refused
with `CameraBusy` naming the holder, sends nothing to the body, and comes back
inside the deadline; that a bus which frees up in time is still *waited for*
rather than refused (the bound must not have become a try-lock); that `preview`
gives up on its own shorter deadline; that `close` refusing leaves the handle
alone and shouts about the USB claim; and that busy is not a disconnect, since
misreading it would drop a healthy connection. These are the one place `FakeClock`
can't help — `threading.Lock`'s timeout reads the real clock regardless of what
`gp2.time` is patched to — so the *bounds* are shrunk instead of the clock, which
is what costs the suite its extra second. The holding thread releases on a 10s
`Event.wait`, so if the bound is ever removed these tests **fail slowly instead of
hanging** — the same insurance as `test_the_bound_holds_on_the_real_clock`.
(2) `BusyHandling` and `ConnectLock` pin the app-side halves: 409 rather than 400
(the value wasn't wrong, and retrying it is the right move) with the connection
kept, a liveview stream that pauses rather than ends when a capture takes the bus,
and a wedged connect that neither hangs `/api/connect` nor kills the watcher.
(3) `test_watchdog.py` covers the heartbeat, and its centre of gravity is
`test_a_wedged_threadpool_withholds_the_ping`: a liveness ping that only proves
the event loop is running would keep the unit looking healthy through exactly the
outage it exists for. `test_the_probe_rides_the_same_pool_the_camera_operations_use`
shrinks anyio's real thread limiter to one token and has a blocking call hold it,
so the claim "the probe is a genuine round trip through the camera pool" is
tested rather than asserted. `SystemdUnit` reads the unit heredoc out of
`tools/setup.sh` as text — the same trick `test_web_contract.py` uses — because a
heartbeat is worthless if the unit never arms `WatchdogSec` or opens
`NotifyAccess`, and neither file can see the other.

**Writes reach the body correctly typed and in order.** `_coerce` is the boundary
between JSON and libgphoto2's C types, and a float where an int belongs is
`[-2] Bad parameters`. The α7 IV's `autofocus` toggle idles at 2 and needs a
press *and* a release; a single write leaves it latched. `manualfocus` is a RANGE
and needs a float. AF-point taps are clamped, then scaled, then formatted — and
the clamp is the only bound, since `AfPoint` accepts any float (a NaN, which
Python's JSON parser will happily accept, collapses to 0 rather than reaching the
body).

**A level the body doesn't offer never reaches USB.** The focus magnifier is the
first action whose valid values are *enumerated by the body* rather than fixed by
the quirk table, so `set_magnifier` validates against the widget's own choices
before writing. Tests pin that an unknown level raises `ValueError` with **no**
`set_single_config` call at all — the same shape as the settings-allowlist round
trip, and for the same reason: the alternative is a `[-2] Bad parameters` from
hardware with no way to tell a typo from a dead bus. The read side is pinned
symmetrically: `levels` comes off the widget, not a constant. `Off` is asserted
to go through `_release_action` (a punched-in body you can't un-punch is the same
class of stranding as a latched shutter) while entering magnification is asserted
*not* to retry — there is nothing to unwind, so the error should surface on the
first attempt.

**The body appends more than it was asked for.** `focusmagnifier` reads back as
`Off,332,249` — level plus the magnifier box's position. A test pins that
`_magnifier_level` strips it, because `value` has to be comparable against
`levels` for the UI to preselect the right option; comparing the raw string would
silently select nothing and the control would look reset after every read.

**A read-back is only as fresh as the path it took.** This was a real field bug,
caught on the rig: the magnifier select displayed every change one selection
behind, because `_read_magnifier` used `get_single_config`, which on Sony serves
a cached property store that a write does not invalidate. The fix is to read
through the whole tree, and `test_the_read_back_goes_through_the_whole_tree_not_single_config`
asserts the *path* — no `get_single_config` after the write — rather than the
value. That is deliberate: no in-process fake can reproduce libgphoto2's cache,
so a value assertion would pass against a fake and fail on the body, exactly the
`changeafarea` trap again. Pinning the call the code makes is the most a unit
test can honestly claim here, and it is enough to stop "switch to the cheaper
single-widget read" from silently reintroducing the bug.

**How many round trips an operation costs is a testable property.** A whole-tree
`get_config()` costs ~416 ms on the α7 IV and holds the bus, so the difference
between one and two of them per magnifier change is the difference between one
and two dozen dropped liveview frames.
`test_a_settled_change_costs_exactly_one_tree_read` counts `get_config` calls,
which is the sort of thing normally left to a profiler and a code review — but
the second read here was invisible (a validation step that looked like a plain
lookup), and it doubled the cost of every change until it was measured on the
rig. Counting calls against the fake is free and catches its return.

**The retry that read count nearly cost us.** Removing that validating read also
removed the settle retry, on the measurement that the retry never fired — a
measurement the removal itself invalidated, since the validating read was what
had been giving the body time to apply the write. The rig came back reading one
level stale on two of three transitions. So the retry is now pinned three ways
against the fake's `lagging_read_back(reads)` hook: a lagging body is **retried**
and reports the level asked for; a body that never reflects the write reports
**what the body says** with a `WARNING` after exactly
`MAGNIFIER_SETTLE_ATTEMPTS` reads; and the retries hold the bus throughout and
sleep a bounded number of times. The `assertEqual(self.clock.sleeps, [])` in the
settled case is the one that matters most — it is what would catch the retry
silently becoming the common path.

**Values are clamped to the widget's own range.** `_coerce` takes the widget, not
just its type, and holds a RANGE value inside the `get_range()` bounds the body
advertises — the only bounds that know where the hardware stops. That one place
covers `/api/focus` and every settings slider, so tests pin both paths plus the
edges: infinities clamp, NaN raises rather than silently collapsing to a bound
(`max(0.0, nan)` returns `0.0`, so an unchecked clamp would drive to the
minimum), each widget is held to its *own* range, and non-RANGE widgets are left
alone. `FocusStep`'s `MAX_FOCUS_STEPS` bound is tested separately and explicitly
as a sanity check, not the hardware limit.

**The disconnect state machine.** This is the one with a field history: a `-52`
during capture used to repeat forever because the camera was never dropped. Tests
cover both directions — transport codes drop the camera, release the USB claim,
and return 503 so the watcher rebuilds it; logical errors (`[-2]`, "busy") return
400/409 and *keep* the connection. `_capture_with_retry` retries `[-1]` but never
a transport error, since retrying on a stale handle only delays the reconnect.

**Connection lifecycle.** A persistent connect failure warns exactly once (the
watcher retries every 3s forever, and warning each time would fill the SD card
while nothing is plugged in); a successful reconnect re-arms that warning. The
watcher does not touch USB while a camera is in use, and does not outlive the
app.

**Quirk resolution.** The project targets ~2,000 models through per-model quirk
tables, so a table that is wrong or incomplete is the archetypal invisible
regression. Tests pin the real α7 IV model string (`Sony Alpha-A7 IV (PC
Control)` — not the internal `ILCE-7M4`), substring/case-insensitive matching for
gphoto2's varying suffix, that vendor tables cover every default key, and that a
fallback to generic widget names is logged loudly rather than silently.

**The settings panel contract.** `list_settings` must not expose read-only
widgets (the browser renders every row as an editable control), unrenderable
widget types, or — importantly — anything from the `actions` section, where the
shutter and focus drives live. `POST /api/settings/{name}` writes then re-reads,
in that order, because the browser re-renders the whole panel from the response.

**The write path accepts exactly what the read path offers.** Both go through
`_settable_widgets`, so the allowlist has one definition and cannot drift. The
tests assert that as a round trip rather than a list: *every* name
`list_settings` returns is writable, and every action/status/read-only/
unrenderable widget is not — including that a refused write reaches no widget at
all, since `POST /api/settings/bulb` used to fire the shutter through an endpoint
meant to be inert.

**Every widget name a quirk table uses exists on the real body.**
`EveryDumpedBodyMatchesItsQuirks` resolves each dump in `fixtures/` through
`gp2._quirks_for` and asserts that every widget the resulting table names is
present in that body's tree. This is the test whose absence let
`af_area_widget: "changeafarea"` — a Canon EOS name — sit in `sony.py` for
months: a wrong name is invisible until it reaches hardware, where it surfaces as
`[-2] Bad parameters` from `get_single_config`, and the hand-written fake
published a `changeafarea` widget *because the quirk table asked for one*. The
AF-point tests were comparing the quirk table against itself. Nothing in the
class is hardcoded to a body, so a new dump is covered on the next run.

**Strict about names the code writes, loose about names it reads.** The two
focus-mode assertions look inconsistent and are not. `af_target_mode` /
`mf_target_mode` are *written* by `_ensure_focus_mode`, so a value the body
doesn't offer is a guaranteed failure the moment focus is used — those must be
real choices. `af_modes` / `mf_modes` are a tolerance list, read only to decide
whether the current mode is already acceptable, and they live in the shared
`GENERAL` table, so they may legitimately name modes other Sony bodies have. The
α7 IV has no `AF-S`, and that is fine. Only a list that misses this body
*entirely* is a bug, because then every focus call would rewrite a mode that was
already correct — so the assertion is a non-empty intersection, not a subset.

**A body no vendor module claims is reported, not failed.** Such a dump falls
through to `DEFAULT_QUIRKS`, whose generic PTP names mostly don't exist on real
hardware, so asserting against it would be noise.
`test_unclaimed_bodies_are_reported` skips with the model name instead — that is
the starting point for adding a camera, not a defect. `vendor_matched()` is what
keeps the two cases apart.

**The fixture directory itself is checked.** `TheFixtureDirectoryIsWellFormed`
runs even with no dumps present, since an empty directory is well-formed. Two
hazards: re-capturing under a different name leaves *two* files for one body
(harmless but confusing — the quirk checks run twice and `fixture_for` answers
with whichever sorted first), and a truncated or failed capture would otherwise
vanish silently, because `fixtures()` skips files with no `Abilities for camera`
header so stray notes don't break the suite. The cost of that tolerance is that a
broken dump looks like an *absent* one, so unparseable `.txt` files are named
explicitly rather than ignored.

## Known gaps

`test_known_gaps.py` holds one test per open hazard from `TODO.md`, each
asserting the behaviour the code *should* have and marked
`@unittest.expectedFailure`. This gives each fix a ready-made acceptance test,
written while the hazard was understood rather than months later.

| Test | TODO | Asserts |
|---|---|---|
| `test_a_vendor_table_need_not_repeat_every_default` | #13 | A vendor table missing a key still yields a complete quirk set. Today `_quirks_for` returns the vendor dict as-is, so an omission is a `KeyError` inside a request handler on someone else's camera. |
| `test_zero_retry_attempts_fails_clearly` | #34 | `capture_retry_attempts = 0` raises something that names the cause. Today the loop body never runs, the function returns `None`, and it surfaces as an `AttributeError` in `_download`. |

When someone fixes one of these, the test stops failing and unittest reports an
**unexpected success — which fails the run**. That is the signal to delete the
decorator and move the test into the module where it belongs. So a red run from
that file means "you fixed something, go promote the test", never "something
broke". Nothing there asserts current behaviour, so no test has to be updated to
keep a known bug alive.

## What is *not* covered

Worth being explicit, because the untested surface is where this project's real
risk sits.

- **Anything about the physical camera.** Whether `focusmode` gates AF the way
  the quirk table claims, whether `shot_gap: 1.5` is long enough, whether the
  status section is really named `status`, whether the AF area grid is really
  640×480 — the tests assert Pathfinder does what the quirk table says, and say
  nothing about whether the table is right. That is a rig question. `TODO.md` #17
  tracks the specific values still unverified.
- **libgphoto2 itself.** The fake reproduces the API's shape, not its behaviour.
  Transport bugs, PTP timing, USB re-enumeration and driver quirks are all
  outside it. (libgphoto2 ships a `vusb` dummy driver that could close some of
  this gap in CI — currently unused.)
- **The frontend at runtime.** No JS runtime on the dev host, no browser
  automation. `test_web_contract.py` covers the seams that break silently — a
  fetch to a route that no longer exists, a `getElementById` for a renamed
  element, a renderer keyed off a widget kind the backend never emits, a bulb
  input whose `max` has drifted from the server's ceiling — by parsing the files
  as text. Actual DOM behaviour, rendering and event handling are untested, as
  are the HTTP methods each call uses.
- **The HTTP layer.** Routes are called as plain async functions, not through a
  client, so this suite does not exercise FastAPI's request parsing, the 422
  response shape, static-file serving, or the multipart stream as a browser
  consumes it. (Using `TestClient` would pull in `httpx`, which the Pi doesn't
  otherwise need.)
- **Concurrency beyond the camera mutex.** One test proves concurrent captures
  serialise, and `BusTimeout` proves everything else is refused rather than
  queued. Nothing covers many clients against one liveview stream (`TODO.md` #10).
- **An operation that is actually stuck.** The bounded-wait tests all use a
  *held* lock, never a hung `libgphoto2` call — nothing here can produce one. So
  the suite pins what happens to the callers queued behind a wedge, not what
  happens to the wedged call itself (nothing: it keeps its worker until the
  process restarts).
- **Deployment.** `tools/setup.sh` as a whole, the AP configuration and the libgphoto2
  build are untested; `SystemdUnit` in `test_watchdog.py` reads four directives
  out of the unit heredoc as text, which is a contract check, not an install test.
  Whether systemd actually aborts and restarts a stopped process is a
  `kill -STOP` on the Pi (`TODO.md` #8).

### `tools/hardware-check.py` — the checks a fake structurally cannot make

Some claims are only decidable against a body, and the magnifier bug is the
canonical one: the fake happily returns whatever it was told, so *every* unit
test passed while the feature was one selection stale on real hardware. No amount
of care in `fakes/` fixes that — a fake that reproduced libgphoto2's property
cache would be reproducing a bug we only learned about *from* the hardware.

`tools/hardware-check.py` is the answer to "how would we have caught this": a
scripted, assertive version of the manual list below, run on the Pi against a
real camera.

```bash
sudo systemctl stop pathfinder      # it holds the USB claim
cd ~/pathfinder && .venv/bin/python tools/hardware-check.py; echo $?
sudo systemctl start pathfinder
```

It prints one line per claim, exits non-zero on any failure, and **fires no
shutter** — so it is safe to run casually, which is the whole point of writing it
down instead of leaving it in a checklist. Each check restores the body's state
in a `finally`, so a failure part-way through does not leave the magnifier
punched in.

Two checks live there today. `check_magnifier` walks every level the body
advertises, asserting each is reported back and survives an independent re-read,
and times a change against the cost of one bare tree read.
`check_read_paths` writes a level and then reads it through **both**
`get_single_config` and `get_config` — the comparison the unit suite genuinely
cannot express, since `test_gp2_camera.py` can pin which call the code *makes*
but not which call tells the truth.

**Its ordering is the entire experiment, and the first version got it wrong.**
That version drove the write with `cam.set_magnifier()`, which ends in a
`get_config()` — so a tree read had already refreshed the property store before
the single-widget read was sampled, and the check reported a clean pass against a
body that was demonstrably stale. It refreshed the state it existed to measure.
The check now writes with `_drive_action` and samples both paths inside **one bus
hold with no `get_config()` in between**. If you extend it, that constraint is
the thing to preserve.

It also splits assertions from measurements, which matters here because "the
single read is stale" is a *finding about this driver*, not a defect in our code
— reporting it as `FAIL` would make a red run the correct outcome and train
people to ignore the exit code. So the freshness of the tree read is an
assertion, while the agreement between the two paths is a `note` that never moves
the exit code. That also makes it a discovery tool: run it on a new body and the
note says immediately whether that vendor's driver behaves the same way.

On the α7 IV (firmware 4.00, 2026-07-31) the corrected check reports
`single='Off' tree='1'` — two reads of the same property microseconds apart,
disagreeing. That single line is what turned TODO #48 from a plausible story
into a measured fact, and it is the whole argument for the tool existing.

It reaches past the camera layer's public surface (`cam._cam`, `cam._quirks`) on
purpose: it is a diagnostic comparing two libgphoto2 paths, which is exactly the
distinction `camera/` exists to hide from everything else. That is the reason it
is a tool and not a test.

### Manual checks the suite can't replace

After changing anything in `camera/`, on the rig:

1. Connect, confirm the status line names the model, confirm a live preview.
2. Capture a still; confirm the file lands in `CAPTURE_DIR` and the shutter fires
   once.
3. Start and stop a recording; confirm the body actually leaves record mode.
4. AF, then a manual focus nudge in both directions; confirm the lens moves and
   `focusmode` ends where you expect.
4b. Punch the magnifier to `5.5×`, nudge focus, then set it back to `Off`.
   `tools/hardware-check.py` now covers the API round trip, so what is left here
   is the part no script can see: that the preview **visibly crops**, and that
   `Off` un-crops it.
5. A short bulb exposure; confirm the shutter closes and the frame downloads.
6. Unplug the camera mid-session; confirm the UI goes to "No camera connected"
   and recovers on replug without restarting the service.
7. Start a long bulb and hit capture from a second browser; confirm the 409 says
   `camera is busy with bulb` within ~2s and the live preview returns by itself
   once the exposure ends.

Step 6 is the one worth doing every time — it is the path with the field bug.

## Adding tests

**For a new camera body:** add its quirks to `camera/sony.py` (or a new vendor
module), then add cases to `test_sony_quirks.py` covering the exact model string
gphoto2 reports for it — `gphoto2 --summary`, or `get_abilities().model`, *not*
the marketing name — plus any override you rely on. If the body's widget names
differ, extend `default_config()` in `fakes/fake_camera.py`. Note that
`test_gp2_helpers.py` already asserts every vendor table covers every key in
`DEFAULT_QUIRKS`, so a new vendor file missing one fails immediately rather than
at request time.

**For a new route:** add the route test to `test_app_routes.py` — the shape to
follow is "success case, wrong-request case (400/409), disconnect case (503, and
assert the camera was dropped)". If the browser calls it, add it to the expected
set in `test_web_contract.py`.

**For a new gphoto2 call:** add the method to `FakeDevice`. `test_fake_fidelity.py`
scans `camera/gp2.py` for the calls it makes and fails if the double is missing
one, so this is enforced rather than remembered.

**Two things worth knowing before writing an async test:** the module-level
`asyncio.Lock` in `app/app.py` binds itself to the first loop that acquires it, and
`asyncio.run()` builds a fresh loop per test — so `RouteTestCase.setUp` replaces
it. And route functions are called directly, which means request bodies are
constructed as pydantic models by hand rather than posted as JSON.
