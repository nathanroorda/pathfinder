#!/usr/bin/env bash
set -euo pipefail

SERVICE="pathfinder"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES="$(cd "$HERE/.." && pwd)/tests/fixtures"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m%s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx \033[0m%s\n' "$*" >&2; exit 1; }

command -v gphoto2 >/dev/null || die "gphoto2 is not installed — run tools/setup.sh first."
mkdir -p "$FIXTURES"

RESTART=0
restore() {
    if [ "$RESTART" = 1 ]; then
        say "Restarting $SERVICE"
        sudo systemctl start "$SERVICE" || warn "could not restart $SERVICE — do it by hand"
    fi
}
cleanup() { rm -f "${RAW:-}"; restore; }

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

keep_raw() {
    local saved="/tmp/camera-dump-failed.txt"
    if [ -s "${RAW:-}" ] && cp "$RAW" "$saved" 2>/dev/null; then
        die "$1 — raw gphoto2 output saved to $saved"
    fi
    die "$1"
}

if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    say "Stopping $SERVICE to release the USB claim"
    sudo systemctl stop "$SERVICE"
    RESTART=1
    sleep 2
else
    say "$SERVICE is not running — nothing to stop"
fi

gphoto2 --auto-detect | grep -q usb: || die "no camera detected. Check the cable is in the Pi's USB port (not PWR), the body is awake, and PC Remote is On."

RAW="$(mktemp)"

say "Dumping configuration (this takes a few seconds)"
gphoto2 --abilities --summary --list-config --list-all-config >"$RAW" 2>&1 \
    || keep_raw "gphoto2 failed"

grep -q "^/main/" "$RAW" \
    || keep_raw "dump has no config tree; is the body in PC Remote mode?"

DRIVER_MODEL="$(sed -n 's/^Abilities for camera *: *//p' "$RAW" | head -1)"
[ -n "$DRIVER_MODEL" ] || die "no 'Abilities for camera' header — dump is unusable"

NAME="${1:-}"
if [ -z "$NAME" ]; then
    for existing in "$FIXTURES"/*.txt; do
        [ -e "$existing" ] || continue
        if [ "$(sed -n 's/^Abilities for camera *: *//p' "$existing" | head -1)" \
             = "$DRIVER_MODEL" ]; then
            NAME="$(basename "$existing")"
            say "Replacing the existing fixture for this body: $NAME"
            break
        fi
    done
fi
if [ -z "$NAME" ]; then
    MODEL="$(sed -n 's/^Model: *//p' "$RAW" | head -1)"
    [ -n "$MODEL" ] || MODEL="$DRIVER_MODEL"
    NAME="$(printf '%s' "$MODEL" | tr '[:upper:]' '[:lower:]' \
            | sed 's/[^a-z0-9]\+/_/g; s/^_//; s/_$//').txt"
    say "New body — creating $NAME"
fi
OUT="$FIXTURES/$NAME"

if [ "${KEEP_SERIAL:-0}" = 1 ]; then
    warn "KEEP_SERIAL=1 — the serial number stays in $NAME"
    cp "$RAW" "$OUT"
else
    awk '
        /^\/main\// { path = $0 }
        /^ *Serial Number:/ { sub(/:.*/, ": <redacted>"); print; next }
        path ~ /serialnumber$/ && /^Current:/ { print "Current: <redacted>"; next }
        { print }
    ' "$RAW" >"$OUT"
fi

[ -s "$OUT" ] || die "wrote an empty fixture to $OUT"

say "Wrote $OUT"
printf '    camera   : %s\n' "$(sed -n 's/^Abilities for camera *: *//p' "$OUT" | head -1)"
printf '    widgets  : %s\n' "$(grep -c '^Readonly:' "$OUT")"
printf '    sections : %s\n' "$(grep '^/main/' "$OUT" | cut -d/ -f3 | sort -u | tr '\n' ' ')"
echo
say "Next: from your dev checkout's repo root, pull it down and run the suite."
echo "    scp $(whoami)@$(hostname):$OUT tests/fixtures/"
echo "    python3 tests/run_tests.py test_fake_fidelity -v"
echo
echo "    (if the hostname doesn't resolve, try $(hostname).local or the Pi's IP)"
