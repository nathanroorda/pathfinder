import os
import threading
import time

import gphoto2 as gp

from . import sony

CAPTURE_DIR = os.environ.get("PATHFINDER_CAPTURE_DIR", "captures")

DEFAULT_QUIRKS = {"shot_gap": 0.0, "capture_retry_attempts": 1}
VENDORS = [sony]

_KIND = {
    gp.GP_WIDGET_RADIO: "choice",
    gp.GP_WIDGET_MENU: "choice",
    gp.GP_WIDGET_TOGGLE: "toggle",
    gp.GP_WIDGET_RANGE: "range",
    gp.GP_WIDGET_TEXT: "text",
}

INCLUDE_SECTIONS = {"imgsettings", "capturesettings", "settings"}


class Gphoto2Camera:
    def __init__(self, cam, model):
        self._cam = cam
        self._lock = threading.Lock()
        self._last_shot = 0.0
        self.model = model
        self._quirks = _quirks_for(model)

    def capture(self, save_dir=CAPTURE_DIR):
        with self._lock:
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
                    time.sleep(1.0)
                    self._drain_events()
                    continue
                raise

    def list_settings(self):
        with self._lock:
            widgets = []
            for section in self._cam.get_config().get_children():
                if section.get_name() in INCLUDE_SECTIONS:
                    widgets += _walk(section)
            return [_describe(w) for w in widgets
                    if w.get_type() in _KIND and not w.get_readonly()]

    def set_setting(self, name, value):
        with self._lock:
            cfg = self._cam.get_config()
            widget = cfg.get_child_by_name(name)
            widget.set_value(_coerce(widget.get_type(), value))
            self._cam.set_config(cfg)


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
            return q
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
    camera._cam.exit()
