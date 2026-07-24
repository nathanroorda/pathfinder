import logging
import os
import threading
import time

import gphoto2 as gp

from . import sony

log = logging.getLogger(__name__)

CAPTURE_DIR = os.environ.get("PATHFINDER_CAPTURE_DIR", "captures")

DEFAULT_QUIRKS = {
    "shot_gap": 0.0,
    "capture_retry_attempts": 1,
    "movie_widget": "movie",
    "af_widget": "autofocusdrive",
    "af_drive_values": (1,),
    "manual_focus_widget": "manualfocusdrive",
    "focus_mode_widget": None,
    "af_modes": (),
    "af_target_mode": None,
    "mf_modes": (),
    "mf_target_mode": None,
}
VENDORS = [sony]

_KIND = {
    gp.GP_WIDGET_RADIO: "choice",
    gp.GP_WIDGET_MENU: "choice",
    gp.GP_WIDGET_TOGGLE: "toggle",
    gp.GP_WIDGET_RANGE: "range",
    gp.GP_WIDGET_TEXT: "text",
}

INCLUDE_SECTIONS = {"imgsettings", "capturesettings", "settings"}
STATUS_SECTIONS = {"status"}

_DISCONNECT_CODES = frozenset(
    getattr(gp, name)
    for name in (
        "GP_ERROR_IO",            # -7  generic I/O failure
        "GP_ERROR_IO_INIT",       # -31 I/O init failed
        "GP_ERROR_IO_READ",       # -34 read failed
        "GP_ERROR_IO_WRITE",      # -35 write failed
        "GP_ERROR_IO_USB_FIND",   # -52 device no longer at its USB address
        "GP_ERROR_IO_USB_CLAIM",  # -53 interface claim lost
    )
    if hasattr(gp, name)
)


class CameraDisconnected(Exception):
    """An operation was attempted on a connection that has been closed."""


def is_disconnect_error(exception):
    if isinstance(exception, CameraDisconnected):
        return True
    return isinstance(exception, gp.GPhoto2Error) and exception.code in _DISCONNECT_CODES


class Gphoto2Camera:
    def __init__(self, cam, model):
        self._cam = cam
        self._lock = threading.Lock()
        self._last_shot = 0.0
        self.model = model
        self._quirks = _quirks_for(model)
        self.recording = False

    def _require_open(self):
        if self._cam is None:
            raise CameraDisconnected("camera connection is closed")

    def close(self):
        with self._lock:
            if self._cam is None:
                return
            cam, self._cam = self._cam, None
            cam.exit()

    def capture(self, save_dir=CAPTURE_DIR):
        with self._lock:
            self._require_open()
            if self.recording:
                raise RuntimeError("cannot capture a still while recording")
            wait = self._quirks["shot_gap"] - (time.monotonic() - self._last_shot)
            if wait > 0:
                time.sleep(wait)
            self._drain_events()
            path = self._capture_with_retry()
            os.makedirs(save_dir, exist_ok=True)
            target = os.path.join(save_dir, f"{int(time.time())}_{path.name}")
            self._cam.file_get(
                path.folder, path.name, gp.GP_FILE_TYPE_NORMAL).save(target)
            self._last_shot = time.monotonic()
            return target

    def preview(self):
        with self._lock:
            self._require_open()
            if self.recording:
                raise RuntimeError("cannot preview while recording")
            camera_file = self._cam.capture_preview()
            return bytes(camera_file.get_data_and_size())

    def _drain_events(self, timeout_ms=200):
        try:
            while self._cam.wait_for_event(timeout_ms)[0] != gp.GP_EVENT_TIMEOUT:
                pass
        except gp.GPhoto2Error:
            pass

    def _capture_with_retry(self):
        attempts = self._quirks["capture_retry_attempts"]
        for i in range(attempts):
            try:
                return self._cam.capture(gp.GP_CAPTURE_IMAGE)
            except gp.GPhoto2Error as exc:
                if exc.code == gp.GP_ERROR and i < attempts - 1:
                    log.warning("capture failed (attempt %d/%d): %r — retrying", i + 1, attempts, exc)
                    time.sleep(1.0)
                    self._drain_events()
                    continue
                raise

    def set_recording(self, on):
        on = bool(on)
        with self._lock:
            self._require_open()
            if on == self.recording:
                return self.recording
            cfg = self._cam.get_config()
            widget = cfg.get_child_by_name(self._quirks["movie_widget"])
            widget.set_value(1 if on else 0)
            self._cam.set_config(cfg)
            self.recording = on
            return self.recording

    def autofocus(self):
        with self._lock:
            self._require_open()
            mode = self._ensure_focus_mode(
                self._quirks["af_modes"], self._quirks["af_target_mode"])
            af = self._quirks["af_widget"]
            for value in self._quirks["af_drive_values"]:
                self._drive_action(af, value)
            return mode

    def manual_focus(self, steps):
        with self._lock:
            self._require_open()
            mode = self._ensure_focus_mode(
                self._quirks["mf_modes"], self._quirks["mf_target_mode"])
            self._drive_action(self._quirks["manual_focus_widget"], steps)
            return mode

    def _ensure_focus_mode(self, acceptable, target):
        name = self._quirks["focus_mode_widget"]
        if not name or not target:
            return None
        cfg = self._cam.get_config()
        widget = cfg.get_child_by_name(name)
        current = widget.get_value()
        if current in acceptable:
            return current
        widget.set_value(target)
        self._cam.set_config(cfg)
        return target

    def _drive_action(self, widget_name, value):
        widget = self._cam.get_single_config(widget_name)
        widget.set_value(_coerce(widget.get_type(), value))
        self._cam.set_single_config(widget_name, widget)

    def list_settings(self):
        with self._lock:
            self._require_open()
            widgets = []
            config = self._cam.get_config()
            for section in config.get_children():
                if section.get_name() in INCLUDE_SECTIONS:
                    widgets += _walk(section)
            return [_describe(w) for w in widgets
                    if w.get_type() in _KIND and not w.get_readonly()]

    def set_setting(self, name, value):
        with self._lock:
            self._require_open()
            cfg = self._cam.get_config()
            widget = cfg.get_child_by_name(name)
            widget.set_value(_coerce(widget.get_type(), value))
            self._cam.set_config(cfg)

    def telemetry(self):
        with self._lock:
            self._require_open()
            widgets = []
            config = self._cam.get_config()
            for section in config.get_children():
                if section.get_name() in STATUS_SECTIONS:
                    widgets += _walk(section)
            return [_describe_status(w) for w in widgets]


def _walk(widget):
    result = []
    for child in widget.get_children():
        if child.get_type() in (gp.GP_WIDGET_WINDOW, gp.GP_WIDGET_SECTION):
            result += _walk(child)
        else:
            result.append(child)
    return result


def _describe(widget):
    kind = _KIND[widget.get_type()]
    info = {
        "name": widget.get_name(),
        "label": widget.get_label(),
        "type": kind,
        "value": widget.get_value(),
    }
    if kind == "choice":
        info["choices"] = [widget.get_choice(i)
                           for i in range(widget.count_choices())]
    elif kind == "range":
        info["min"], info["max"], info["step"] = widget.get_range()
    return info


def _describe_status(widget):
    try:
        value = widget.get_value()
    except gp.GPhoto2Error:
        value = None
    return {
        "name": widget.get_name(),
        "label": widget.get_label(),
        "value": value,
    }


def _coerce(widget_type, value):
    if widget_type == gp.GP_WIDGET_RANGE:
        return float(value)
    if widget_type == gp.GP_WIDGET_TOGGLE:
        return int(value)
    return str(value)


def _quirks_for(model):
    for vendor in VENDORS:
        q = vendor.quirks(model)
        if q is not None:
            log.info("matched vendor quirks for model %r", model)
            return q
    log.warning(
        "no vendor quirks matched model %r — using generic defaults; "
        "vendor-specific actions (focus, etc.) may not work", model)
    return DEFAULT_QUIRKS


def connect():
    cam = gp.Camera()
    cam.init()
    model = "USB camera (gphoto2)"
    try:
        model = cam.get_abilities().model or model
    except gp.GPhoto2Error:
        pass
    return Gphoto2Camera(cam, model)


def disconnect(camera):
    camera.close()
