# Testing

Pathfinder's test suite: 251 tests, `unittest` only, no third-party test
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
│   └── fake_camera.py    in-memory device, widget tree, and app-layer double
├── test_sony_quirks.py     9   vendor/model quirk resolution
├── test_gp2_helpers.py    37   coercion, clamping, describe, disconnect codes
├── test_gp2_camera.py    101   Gphoto2Camera against a fake device
├── test_app_models.py     27   request validation + the bulb ceiling
├── test_app_routes.py     53   routes, error mapping, connection state machine
├── test_web_contract.py    9   web/ ↔ app.py seams (static text checks)
├── test_fake_fidelity.py  13   does the double still resemble the real thing
└── test_known_gaps.py      2   TODO.md hazards, as expected-to-fail tests
```

## Running it

From the repo root:

```bash
python3 -m unittest discover -t . -s tests      # everything
python3 -m unittest tests.test_gp2_camera -v    # one module
python3 -m unittest tests.test_gp2_camera.Bulb  # one class
python3 tests/run_tests.py -v                   # same, from any directory
```

`run_tests.py` exists only to remove a footgun: `app.py` mounts
`StaticFiles(directory="web")` at import time, so anything that imports it must
run with the repo root as the working directory. The runner (and `support.py`)
handles that for you.

### On the dev host vs. on the Pi

The dev machine has no pip, no venv, and no `gphoto2`/`fastapi`, so **80 tests
skip there** — everything that needs FastAPI or pydantic — plus the 5 that
compare the fake against the real binding. The remaining 166 execute, including
the entire camera layer:

```
Ran 251 tests in 0.18s
OK (skipped=85, expected failures=2)
```

To run the whole suite, use the Pi's venv (created by `setup.sh`), which has the
real dependencies:

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

**`FakeWidget` / `default_config()`** — a config tree shaped like the α7 IV's.
Section placement matters: `INCLUDE_SECTIONS` decides what `list_settings`
exposes, `STATUS_SECTIONS` what `telemetry` does, and `actions` must be excluded
from both while still being reachable by `get_child_by_name` (which searches the
entire tree). The tree deliberately includes a read-only widget, a
`GP_WIDGET_BUTTON`, a nested section, and a status widget that raises on
`get_value` — each one exists to pin a filtering rule.

**`FakeDevice`** — what `gp.Camera()` returns, i.e. the object `Gphoto2Camera`
wraps. It reproduces libgphoto2's *snapshot* config model: `get_config()` returns
a detached copy, edits to that copy are local, and nothing reaches the device
until `set_config()` pushes it back. That distinction is load-bearing —
"`_ensure_focus_mode` leaves an acceptable mode alone" is only a meaningful
assertion if a forgotten `set_config` would be observable.

**`FakeConnectedCamera`** — the app-layer double. `app.py` never sees a
`FakeDevice`; it holds whatever `camera.connect()` returned and only calls
`Gphoto2Camera`'s public surface. That surface is an undeclared duck-typed
interface, so `test_fake_fidelity.py` pins both ends of it: the real camera must
answer every call `app.py` makes, and so must the double.

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

**Writes reach the body correctly typed and in order.** `_coerce` is the boundary
between JSON and libgphoto2's C types, and a float where an int belongs is
`[-2] Bad parameters`. The α7 IV's `autofocus` toggle idles at 2 and needs a
press *and* a release; a single write leaves it latched. `manualfocus` is a RANGE
and needs a float. AF-point taps are clamped, then scaled, then formatted — and
the clamp is the only bound, since `AfPoint` accepts any float (a NaN, which
Python's JSON parser will happily accept, collapses to 0 rather than reaching the
body).

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
  serialise. Nothing covers many clients against one liveview stream (`TODO.md`
  #10) or the absence of any operation timeout (#8).
- **Deployment.** `setup.sh`, the AP configuration, the systemd unit and the
  libgphoto2 build are untested.

### Manual checks the suite can't replace

After changing anything in `camera/`, on the rig:

1. Connect, confirm the status line names the model, confirm a live preview.
2. Capture a still; confirm the file lands in `CAPTURE_DIR` and the shutter fires
   once.
3. Start and stop a recording; confirm the body actually leaves record mode.
4. AF, then a manual focus nudge in both directions; confirm the lens moves and
   `focusmode` ends where you expect.
5. A short bulb exposure; confirm the shutter closes and the frame downloads.
6. Unplug the camera mid-session; confirm the UI goes to "No camera connected"
   and recovers on replug without restarting the service.

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
`asyncio.Lock` in `app.py` binds itself to the first loop that acquires it, and
`asyncio.run()` builds a fresh loop per test — so `RouteTestCase.setUp` replaces
it. And route functions are called directly, which means request bodies are
constructed as pydantic models by hand rather than posted as JSON.
