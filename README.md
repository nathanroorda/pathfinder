# Pathfinder

Control a tethered camera from your phone's browser — no app to install, no internet connection needed.

## Overview

Pathfinder turns a Raspberry Pi into a self-contained WiFi remote for a camera. The Pi hosts its own WiFi network; any phone or laptop that joins it gets a browser page showing the camera's live settings and a capture button. Plug in a different camera and the settings panel rebuilds itself from whatever that camera reports — nothing is hardcoded to one model.

## Supported Cameras

Pathfinder runs on [libgphoto2](https://github.com/gphoto/libgphoto2), which supports roughly 2,000 camera models. Any of them should work out of the box.

The list below is what's actually been tested end-to-end and tuned for (see `camera/sony.py`). It'll grow as more cameras are verified.

| Camera | Status |
|---|---|
| Sony ILCE-7M4 (α7 IV) | ✅ Tested & supported |

## Getting Started

For a Pathfinder device that's already been provisioned:

1. Power on the Pi.
2. On your phone, join the WiFi network **Pathfinder** (password: `pathfinder`).
3. Open `http://10.42.0.1:8080` in a browser.
4. Plug the camera into the Pi's USB port and power it on. If the camera has a "PC Remote" / tether mode, enable it.
5. The status line should read **Connected: \<camera model\>** within a few seconds, and a live preview appears.
6. Use **AF** / the **◀ ▶** focus nudges to focus — or tap the live preview to move the AF point to that spot — adjust settings, and tap **Capture** for a still or **Record** to start/stop video.
7. For a long exposure, put the body in **Bulb** mode, enter a duration next to **Bulb**, and tap it. The preview pauses for the exposure and returns on its own afterwards.

## File Layout

<details>
<summary>Click to expand</summary>

```
.
├── TODO.md           Known issues and open work, by severity
├── app/
│   ├── __init__.py  Empty on purpose — see app.md ("Why app/__init__.py is empty")
│   ├── app.py       FastAPI app: HTTP routes + serves the web UI
│   └── app.md       Backend architecture: lifecycle, timeouts, the HTTP API table
├── camera/
│   ├── __init__.py  Public interface: connect() / disconnect()
│   ├── gp2.py       libgphoto2 backend: capture, liveview, recording, focus, settings
│   ├── sony.py      Per-model quirks (timing, retry, focus widgets & modes)
│   └── camera.md    Camera-layer internals: the bus lock, quirks, disconnect handling
├── logs/
│   ├── __init__.py  Public interface: configure_logging()
│   ├── log.py       Logging setup (called first, before anything is imported)
│   ├── log.md       Logging architecture: levels, journald, persistence caveats
│   └── *.log        Runtime log files (gitignored)
├── web/
│   ├── index.html  Page shell
│   ├── script.js   Status polling, capture button, settings rendering
│   ├── style.css   Styling
│   └── web.md      Frontend behavior and the widget-descriptor contract it renders
├── tools/
│   ├── run.py            Entry point — starts the server (python tools/run.py)
│   ├── requirements.txt  Python dependencies
│   ├── setup.sh          Provisions a fresh Raspberry Pi (see "Provisioning" below)
│   └── camera-dump.sh    Captures a camera's config as a test fixture
└── tests/
    ├── tests.md    How the suite works and what it deliberately misses
    ├── fakes/      A fake libgphoto2 — the suite needs no camera
    ├── fixtures/   Real gphoto2 dumps, captured off hardware
    └── test_*.py   unittest modules
```

Each `.md` sits next to the code it documents. Start with **`app/app.md`** for the
backend, **`camera/camera.md`** for anything touching the camera, and
**`TODO.md`** before changing behavior — it records why several things are the
way they are.

</details>

---

## Architecture

- **`app/app.py` / `tools/run.py`** — a FastAPI app exposing a small REST API (`/api/status`, `/api/connect`, `/api/capture`, `/api/bulb`, `/api/liveview`, `/api/record/*`, `/api/autofocus`, `/api/focus`, `/api/afpoint`, `/api/telemetry`, `/api/settings`) and serving `web/` as static files. Runs under `uvicorn`. Every route is documented with its error cases in **`app/app.md`**.
- **`camera/`** — wraps the `gphoto2` Python binding. `gp2.py` handles connecting, capturing, live preview, recording, focus, and reading/writing settings; `sony.py` holds per-model quirks (timing, retry, focus widgets & modes) looked up by camera model string.
- **`logs/`** — logging setup, and the directory runtime `*.log` files land in (gitignored). `tools/run.py` calls `configure_logging()` as its very first statement, before `app`/`camera` are imported, so their import-time log lines aren't lost. See **`logs/log.md`**.
- **`web/`** — a small vanilla JS/HTML/CSS frontend. It shows a live preview and focus controls, renders whatever settings the connected camera reports (choice/toggle/range/text controls, built dynamically from the API response), and posts changes back.
- **`tests/`** — a `unittest` suite that runs against a fake libgphoto2, so no camera (and no `gphoto2` install) is needed. See **`tests/tests.md`**.

## Tests

```
python3 -m unittest discover -t . -s tests    # from the repo root
python3 tests/run_tests.py -v                 # same, from anywhere
```

No third-party test dependencies. On a machine without `fastapi`/`pydantic` the request-layer tests skip and the rest still run; on the Pi, use `.venv/bin/python` to run everything. Details, coverage and the manual hardware checks the suite can't replace are in **`tests/tests.md`**.

## Provisioning a New Device

`tools/setup.sh` takes a freshly flashed Raspberry Pi OS to a working Pathfinder. Run it once per device, before shipping.

**Before running it:** use Raspberry Pi Imager's advanced options to pre-configure your home WiFi and enable SSH, so the Pi is reachable without a monitor/keyboard.

```
ssh <user>@<pi-on-your-network>
git clone https://github.com/nathanroorda/pathfinder.git
cd pathfinder
./tools/setup.sh
```

It resolves the project root from its own location, so it works from any working
directory — but it must stay in `tools/`, one level below the root.

(HTTPS, not SSH — a freshly imaged Pi has no key on it yet.)

What it does — nine steps, re-runnable (each is safe to run again):

1. Installs system + build packages.
2. Builds `libgphoto2` from source (the packaged version has a known Sony regression).
3. Builds the `gphoto2` **CLI** from source too, against that library — not needed by the app, but it's the tool you'll debug a new body with (`gphoto2 --list-all-config`).
4. Removes the old apt-packaged `libgphoto2` so the source build isn't shadowed.
5. Generates udev rules + a hwdb entry covering every camera libgphoto2 supports, and grants USB access via the `plugdev` group — no per-camera configuration needed.
6. Creates a Python venv and installs dependencies.
7. Rebuilds the `gphoto2` Python binding from source against the source-built library.
8. Creates the **Pathfinder** WiFi access point (NetworkManager hotspot at `10.42.0.1`).
9. Installs and enables the `pathfinder` systemd service (starts `tools/run.py` on boot, under a 30s systemd watchdog — see below).

It finishes by connecting to the camera and taking one test frame, so expect the shutter to fire once.

Provisioning env toggles:
- `FORCE_BUILD=1` — rebuild libgphoto2 even if already installed.
- `AP_ON_BOOT=0` — create the AP profile but don't auto-start it on boot (keeps a home-WiFi fallback for development).

The script does **not** wrap itself in a terminal multiplexer, so a dropped SSH
session will kill a half-finished run. Over an unreliable link, start it inside
one yourself:

```
tmux new -s setup ./tools/setup.sh     # reattach with: tmux attach -t setup
```

After it finishes, reboot — the AP and app both come up automatically.

Service management:
```
sudo systemctl {start,stop,status,restart} pathfinder
journalctl -u pathfinder -f
```

### Runtime configuration

Read from the environment at process start, so changing one needs a service
restart. Set them via a systemd drop-in (`tools/setup.sh` regenerates the unit
file on every run, so an in-place edit gets overwritten) — `logs/log.md` has the
recipe.

| Variable | Default | Effect |
|---|---|---|
| `PATHFINDER_LOG_LEVEL` | `DEBUG` | Root log level. `DEBUG` is verbose enough to matter for SD-card wear on a long deployment. |
| `PATHFINDER_CAPTURE_DIR` | `captures` | Where downloaded frames land. Relative paths resolve against the service's `WorkingDirectory`, i.e. the repo. |
| `PATHFINDER_MAX_BULB_SECONDS` | `900` | Ceiling on a single bulb exposure. The server rejects anything above it with a 422 before touching hardware — this bounds how long a bad request can hold the shutter open. Note the UI's input still caps at the compiled-in default of 900, so raising this only takes effect for direct API calls unless you also edit `web/index.html`. |

### The watchdog

The systemd unit sets `WatchdogSec=30` and `Restart=always`. The app pings
systemd every 15s, but **only** after a round trip through the same thread pool
the camera operations use — so a wedged `libgphoto2` call (which cannot be
interrupted from Python) withholds the ping and systemd restarts the process
within ~45s. This is why the service may restart itself with no error in the
log; see the Troubleshooting note below, and `app/app.md` for the reasoning.

## Troubleshooting

- **Camera not detected** — confirm it's powered on, in "PC Remote" / tether mode if it has one, and connected to the Pi's data/USB port (not a charge-only port). Re-run `sudo udevadm trigger` or unplug/replug.
- **Can't reach `10.42.0.1`** — make sure your phone actually joined the **Pathfinder** network, not your home WiFi (the Pi only has one radio, so it can't host both at once).
- **App not responding after boot** — check `journalctl -u pathfinder -f` for errors, and confirm the service is active with `systemctl status pathfinder`.
- **The service keeps restarting, with nothing in the log** — that's the watchdog. Look for `Watchdog timeout` in `journalctl -u pathfinder`, and for the `threadpool has not answered a liveness probe` line just before it, which means a camera operation wedged. Unplug the camera and see whether the service settles; if it does, the body wedged the USB transaction rather than the app crashing. (A restart loop with *no* watchdog message and no `camera connected` line is a wedged connect at startup — `TODO.md` #39.)
- **`Connected: <model>` but every button returns an error and the settings panel is empty** — the camera is in MTP/Mass Storage mode, not PC Remote. The connection succeeds; there just aren't any controls behind it.

## Notes

- The Pi has a single WiFi radio: the **Pathfinder** access point and a home-WiFi connection are mutually exclusive (set `AP_ON_BOOT=0` during development to keep the home-WiFi fallback).
- Only the Sony α7 IV has been field-tested; other libgphoto2-supported cameras should work but haven't been verified.
