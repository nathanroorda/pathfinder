#!/usr/bin/env bash
set -euo pipefail
 
LIBGPHOTO2_REPO="https://github.com/gphoto/libgphoto2.git"
BUILD_DIR="$HOME/libgphoto2"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
 
AP_CONN="pathfinder-ap"
AP_SSID="Pathfinder"
AP_PASS="pathfinder"
AP_CHANNEL="6"
AP_ON_BOOT="${AP_ON_BOOT:-1}"
 
SERVICE="pathfinder"
TMUX_SESSION="pathfinder-setup"
SCRIPT_PATH="$PROJECT_DIR/$(basename "${BASH_SOURCE[0]}")"
 
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
 
if [ -z "${TMUX:-}" ] && [ -z "${PATHFINDER_NO_TMUX:-}" ]; then
  if ! command -v tmux >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y tmux || true
  fi
  if ! command -v tmux >/dev/null 2>&1; then
    warn "tmux unavailable — continuing without crash protection."
  elif tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "A setup session is already running — attaching you to it."
    exec tmux attach -t "$TMUX_SESSION"
  else
    echo "Launching setup inside tmux (session: $TMUX_SESSION)."
    echo "If your connection drops, reconnect and run: tmux attach -t $TMUX_SESSION"
    exec tmux new-session -s "$TMUX_SESSION" -- "$SCRIPT_PATH" "$@"
  fi
fi
 
[ "$EUID" -ne 0 ] || die "Run as your normal user, not root/sudo — the venv must be yours."
[ -f "$PROJECT_DIR/requirements.txt" ] || die "Run from the project root (requirements.txt not found)."
sudo -v || die "This script needs sudo access."
 
say "1/8  System update + build & runtime packages"
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-dev \
  git build-essential autoconf automake libtool pkg-config autopoint gettext \
  libltdl-dev libusb-1.0-0-dev libexif-dev libjpeg-dev libgd-dev
 
say "2/8  Build libgphoto2 from source"
if [ -f /usr/local/lib/libgphoto2.so ] && [ "${FORCE_BUILD:-0}" != "1" ]; then
  warn "libgphoto2 already in /usr/local — skipping build (FORCE_BUILD=1 to rebuild)."
else
  if [ -d "$BUILD_DIR/.git" ]; then
    git -C "$BUILD_DIR" pull --ff-only || true
  else
    git clone "$LIBGPHOTO2_REPO" "$BUILD_DIR"
  fi
  (
    cd "$BUILD_DIR"
    autoreconf -is
    ./configure --prefix=/usr/local
    make -j"$(nproc)"
    sudo make install
  )
  sudo ldconfig
fi
 
say "3/8  Remove old apt libgphoto2 (would otherwise shadow the build)"
PURGE=()
for pkg in libgphoto2-6t64 libgphoto2-port12t64; do
  if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then PURGE+=("$pkg"); fi
done
if [ "${#PURGE[@]}" -gt 0 ]; then
  sudo apt remove --purge -y "${PURGE[@]}"
else
  warn "old apt libs already absent — nothing to purge."
fi
echo "/usr/local/lib" | sudo tee /etc/ld.so.conf.d/usrlocal.conf >/dev/null
sudo ldconfig
ldconfig -p | grep -q "/usr/local/lib/libgphoto2.so" \
  || die "libgphoto2 not resolving from /usr/local — build/install problem."
 
say "4/8  Camera USB permissions (udev rules for every camera libgphoto2 supports)"
PRINT_CAMERA_LIST="$(find /usr/local -type f -name print-camera-list | head -n1)"
CHECK_PTP_CAMERA="$(find /usr/local -type f -name check-ptp-camera | head -n1)"
[ -n "$PRINT_CAMERA_LIST" ] && [ -n "$CHECK_PTP_CAMERA" ] \
  || die "print-camera-list/check-ptp-camera not found — libgphoto2 build/install problem."
sudo rm -f /etc/udev/rules.d/90-libgphoto2.rules
sudo install -Dm755 "$CHECK_PTP_CAMERA" /lib/udev/check-ptp-camera
"$PRINT_CAMERA_LIST" udev-rules version 201 group plugdev | sudo tee /etc/udev/rules.d/60-libgphoto2.rules >/dev/null
"$PRINT_CAMERA_LIST" hwdb | sudo tee /etc/udev/hwdb.d/20-libgphoto2.hwdb >/dev/null
sudo systemd-hwdb update || sudo udevadm hwdb --update
sudo usermod -aG plugdev "$USER"
sudo udevadm control --reload-rules
sudo udevadm trigger
 
say "5/8  Python venv + dependencies"
cd "$PROJECT_DIR"
[ -d .venv ] || python3 -m venv .venv
set +u; source .venv/bin/activate; set -u
pip install --upgrade pip
pip install -r requirements.txt
 
say "6/8  Build the gphoto2 Python binding against /usr/local"
PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
LDFLAGS=-L/usr/local/lib \
CFLAGS=-I/usr/local/include \
pip install --no-binary :all: --force-reinstall --no-cache-dir gphoto2
 
say "7/8  WiFi access point profile ($AP_SSID)"
if nmcli -g NAME con show | grep -qx "$AP_CONN"; then
  warn "$AP_CONN already exists — updating its settings."
else
  sudo nmcli con add type wifi ifname wlan0 mode ap con-name "$AP_CONN" ssid "$AP_SSID"
fi
sudo nmcli con modify "$AP_CONN" 802-11-wireless.band bg
sudo nmcli con modify "$AP_CONN" 802-11-wireless.channel "$AP_CHANNEL"
sudo nmcli con modify "$AP_CONN" wifi-sec.key-mgmt wpa-psk
sudo nmcli con modify "$AP_CONN" wifi-sec.psk "$AP_PASS"
sudo nmcli con modify "$AP_CONN" ipv4.method shared
sudo nmcli con modify "$AP_CONN" ipv6.method disabled
sudo nmcli con modify "$AP_CONN" connection.autoconnect no
 
say "8/8  Install systemd service ($SERVICE.service)"
SERVICE_GROUP="$(id -gn "$USER")"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Pathfinder camera controller
After=network.target
 
[Service]
Type=simple
User=$USER
Group=$SERVICE_GROUP
SupplementaryGroups=plugdev
WorkingDirectory=$PROJECT_DIR
Environment=LD_LIBRARY_PATH=/usr/local/lib
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/run.py
Restart=on-failure
RestartSec=3
 
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE.service"
warn "Service enabled for boot. It is NOT started now (a reboot starts it"
warn "cleanly alongside the AP). Manual control: sudo systemctl {start,stop,status,restart} $SERVICE"
 
say "Provisioning complete."
if id -nG | grep -qw plugdev; then
  say "Verifying camera (must be connected, powered, PC Remote: On, cable in the USB port)"
  python -c "
import camera
cam = camera.connect()
print('Connected:', cam.model)
print('Captured:', cam.capture())
camera.disconnect(cam)
" || warn "Verify failed — check the camera is awake, PC Remote: On, and the cable is in the Pi's 'USB' port (not 'PWR')."
else
  warn "You were just added to the 'plugdev' group — it applies on next login/reboot."
fi
 
say "Finalizing WiFi access point boot behavior"
if [ "$AP_ON_BOOT" = "1" ]; then
  sudo nmcli con modify "$AP_CONN" connection.autoconnect yes
  sudo nmcli con modify "$AP_CONN" connection.autoconnect-priority 100
  warn "AP is set to start on boot. After reboot the Pi hosts \"$AP_SSID\" and"
  warn "is reachable ONLY at 10.42.0.1 (join that network, not home WiFi)."
else
  warn "AP will NOT auto-start (AP_ON_BOOT=0). Bring it up manually with:"
  warn "    sudo nmcli con up $AP_CONN"
fi
 
cat <<NEXT
 
Done. To bring up the access point AND the app together, reboot:
 
    sudo reboot
 
After it comes back up (~30-60s), from a phone/laptop:
    1. Join WiFi "$AP_SSID"  (password: $AP_PASS)
    2. Open  http://10.42.0.1:8080
 
To reach the Pi over SSH once it's in AP mode:
    ssh $USER@10.42.0.1
 
Service logs (for troubleshooting the app on boot):
    journalctl -u $SERVICE -f
NEXT
 
