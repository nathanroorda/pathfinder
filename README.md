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
5. The status line should read **Connected: \<camera model\>** within a few seconds.
6. Adjust settings and tap **Capture**.

## File Layout

<details>
<summary>Click to expand</summary>

```
.
├── app.py            FastAPI app: HTTP routes + serves the web UI
├── run.py            Entry point — starts the server (python run.py)
├── requirements.txt  Python dependencies
├── setup.sh          Provisions a fresh Raspberry Pi (see "Provisioning" below)
├── camera/
│   ├── __init__.py  Public interface: connect() / disconnect()
│   ├── gp2.py       libgphoto2 backend: capture, settings, connection handling
│   └── sony.py      Per-model quirks (shot timing, retry behavior)
└── web/
    ├── index.html  Page shell
    ├── script.js   Status polling, capture button, settings rendering
    └── style.css   Styling
```

</details>

---

## Architecture

- **`app.py` / `run.py`** — a FastAPI app exposing a small REST API (`/api/status`, `/api/connect`, `/api/capture`, `/api/settings`) and serving `web/` as static files. Runs under `uvicorn`.
- **`camera/`** — wraps the `gphoto2` Python binding. `gp2.py` handles connecting, capturing, and reading/writing settings; `sony.py` holds per-model timing/retry quirks looked up by camera model string.
- **`web/`** — a small vanilla JS/HTML/CSS frontend. It polls `/api/status`, renders whatever settings the connected camera reports (choice/toggle/range/text controls, built dynamically from the API response), and posts changes back.

## Provisioning a New Device

`setup.sh` takes a freshly flashed Raspberry Pi OS to a working Pathfinder. Run it once per device, before shipping.

**Before running it:** use Raspberry Pi Imager's advanced options to pre-configure your home WiFi and enable SSH, so the Pi is reachable without a monitor/keyboard.

```
ssh <user>@<pi-on-your-network>
git clone git@github.com:nathanroorda/pathfinder.git
cd pathfinder
./setup.sh
```

What it does (re-runnable — each step is safe to run again):

1. Installs system + build packages.
2. Builds `libgphoto2` from source (the packaged version has a known Sony regression).
3. Removes the old apt-packaged `libgphoto2` so the source build isn't shadowed.
4. Generates udev rules + a hwdb entry covering every camera libgphoto2 supports, and grants USB access via the `plugdev` group — no per-camera configuration needed.
5. Creates a Python venv and installs dependencies.
6. Builds the `gphoto2` Python binding against the source-built library.
7. Creates the **Pathfinder** WiFi access point (NetworkManager hotspot at `10.42.0.1`).
8. Installs and enables the `pathfinder` systemd service (starts `run.py` on boot).

Env toggles:
- `FORCE_BUILD=1` — rebuild libgphoto2 even if already installed.
- `AP_ON_BOOT=0` — create the AP profile but don't auto-start it on boot (keeps a home-WiFi fallback for development).

After it finishes, reboot — the AP and app both come up automatically.

Service management:
```
sudo systemctl {start,stop,status,restart} pathfinder
journalctl -u pathfinder -f
```

## Troubleshooting

- **Camera not detected** — confirm it's powered on, in "PC Remote" / tether mode if it has one, and connected to the Pi's data/USB port (not a charge-only port). Re-run `sudo udevadm trigger` or unplug/replug.
- **Can't reach `10.42.0.1`** — make sure your phone actually joined the **Pathfinder** network, not your home WiFi (the Pi only has one radio, so it can't host both at once).
- **App not responding after boot** — check `journalctl -u pathfinder -f` for errors, and confirm the service is active with `systemctl status pathfinder`.

## Notes

- The Pi has a single WiFi radio: the **Pathfinder** access point and a home-WiFi connection are mutually exclusive (set `AP_ON_BOOT=0` during development to keep the home-WiFi fallback).
- Only the Sony α7 IV has been field-tested; other libgphoto2-supported cameras should work but haven't been verified.
