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
3. Open `http://pathfinder.local:8080` in a browser (or `http://10.42.0.1:8080`).
4. Plug the camera into the Pi's USB port and power it on. If the camera has a "PC Remote" / tether mode, enable it.
5. The status line should read **Connected: \<camera model\>** within a few seconds.
6. Adjust settings and tap **Capture**.

### Switching Networks

The **Network** section of the page lets the Pi join any WiFi network, or switch back to hosting its own:

- Enter an SSID/password and tap **Join this network** — the Pi saves it and attempts to join. If it succeeds, reconnect your device to that same network and reload `pathfinder.local:8080`. If the password's wrong or the network's out of range, the Pi automatically falls back to hosting **Pathfinder** again within ~20s.
- Tap **Use Pathfinder AP** to switch back to the Pi's own hotspot at any time.
- `pathfinder.local` resolves regardless of which network the Pi is currently on (via mDNS/avahi) — bookmark that instead of an IP.

## File Layout

<details>
<summary>Click to expand</summary>

```
.
├── app.py            FastAPI app: HTTP routes + serves the web UI
├── network.py        nmcli wrapper: AP/home WiFi status and switching
├── run.py            Entry point — starts the server (python run.py)
├── requirements.txt  Python dependencies
├── setup.sh          Provisions a fresh Raspberry Pi (see "Provisioning" below)
├── camera/
│   ├── __init__.py  Public interface: connect() / disconnect()
│   ├── gp2.py       libgphoto2 backend: capture, settings, connection handling
│   └── sony.py      Per-model quirks (shot timing, retry behavior)
└── web/
    ├── index.html  Page shell
    ├── script.js   Status polling, capture button, settings rendering, network switching
    └── style.css   Styling
```

</details>

---

## Architecture

- **`app.py` / `run.py`** — a FastAPI app exposing a small REST API (`/api/status`, `/api/connect`, `/api/capture`, `/api/settings`, `/api/network/*`) and serving `web/` as static files. Runs under `uvicorn`.
- **`camera/`** — wraps the `gphoto2` Python binding. `gp2.py` handles connecting, capturing, and reading/writing settings; `sony.py` holds per-model timing/retry quirks looked up by camera model string.
- **`network.py`** — wraps `nmcli` to report the active WiFi mode (`pathfinder-ap` vs. a separate `pathfinder-home` profile) and switch between them. A switch to home WiFi that doesn't fully associate within a timeout automatically reverts to the AP, so the Pi can't strand itself.
- **`web/`** — a small vanilla JS/HTML/CSS frontend. It polls `/api/status`, renders whatever settings the connected camera reports (choice/toggle/range/text controls, built dynamically from the API response), posts changes back, and polls `/api/network/status` to drive the network-switching form.

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
8. Sets up mDNS (`pathfinder.local`) and a scoped passwordless-sudo rule for `nmcli`, so the app can switch WiFi networks itself (see [Switching Networks](#switching-networks)).
9. Installs and enables the `pathfinder` systemd service (starts `run.py` on boot).

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
- **Can't reach `pathfinder.local` or `10.42.0.1`** — make sure your device actually joined the **Pathfinder** network, not your home WiFi (the Pi only has one radio, so it can't host both at once). `pathfinder.local` only resolves for devices on the same local network as the Pi.
- **App not responding after boot** — check `journalctl -u pathfinder -f` for errors, and confirm the service is active with `systemctl status pathfinder`.
- **Stuck after entering home WiFi credentials** — the Pi should fall back to hosting **Pathfinder** within ~20s if it can't join (wrong password, out of range); rejoin **Pathfinder** and check `/api/network/status`. If it's still stuck, `ssh` in over whichever network it did land on and run `sudo nmcli con up pathfinder-ap`.

## Notes

- The Pi has a single WiFi radio: the **Pathfinder** access point and a home-WiFi connection are mutually exclusive. The app's Network section (or `AP_ON_BOOT=0` during provisioning) switches between them; only one is active at a time.
- Only the Sony α7 IV has been field-tested; other libgphoto2-supported cameras should work but haven't been verified.
