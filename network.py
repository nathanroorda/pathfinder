import logging
import subprocess

log = logging.getLogger(__name__)

AP_CONN = "pathfinder-ap"
HOME_CONN = "pathfinder-home"


def _nmcli(*args: str, wait: int | None = None) -> subprocess.CompletedProcess:
    cmd = ["sudo", "nmcli"]
    if wait is not None:
        cmd += ["-w", str(wait)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _active_conn_on_wlan0() -> str | None:
    result = _nmcli("-t", "-f", "NAME,DEVICE", "con", "show", "--active")
    for line in result.stdout.splitlines():
        name, _, device = line.partition(":")
        if device == "wlan0":
            return name
    return None


def status() -> dict:
    active = _active_conn_on_wlan0()
    if active == AP_CONN:
        mode = "ap"
    elif active == HOME_CONN:
        mode = "home"
    else:
        mode = "unknown"

    home_ssid = None
    result = _nmcli("-g", "802-11-wireless.ssid", "con", "show", HOME_CONN)
    if result.returncode == 0:
        home_ssid = result.stdout.strip() or None

    return {"mode": mode, "home_ssid": home_ssid}


def connect_ap(wait: int = 10) -> bool:
    result = _nmcli("con", "up", AP_CONN, wait=wait)
    if result.returncode != 0:
        log.warning("failed to activate %s: %s", AP_CONN, result.stderr.strip())
        return False
    return True


def connect_home(ssid: str, password: str, wait: int = 20) -> bool:
    exists = _nmcli("con", "show", HOME_CONN).returncode == 0
    if exists:
        _nmcli(
            "con", "modify", HOME_CONN,
            "802-11-wireless.ssid", ssid,
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", password,
        )
    else:
        _nmcli(
            "con", "add", "type", "wifi", "ifname", "wlan0",
            "con-name", HOME_CONN, "ssid", ssid,
        )
        _nmcli(
            "con", "modify", HOME_CONN,
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", password,
        )

    result = _nmcli("con", "up", HOME_CONN, wait=wait)
    if result.returncode != 0:
        log.warning("failed to join %r within %ss: %s — reverting to %s",
                     ssid, wait, result.stderr.strip(), AP_CONN)
        connect_ap()
        return False
    return True
