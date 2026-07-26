import gphoto2 as gp


class FakeWidget:
    def __init__(self, name, wtype, value=None, label=None, choices=(),
                 rng=None, readonly=False, children=(), value_error=None):
        self.name = name
        self.wtype = wtype
        self.value = value
        self.label = label if label is not None else name.replace("-", " ").title()
        self.choices = tuple(choices)
        self.rng = rng
        self.readonly = readonly
        self.children = list(children)
        self.value_error = value_error

    def get_name(self):
        return self.name

    def get_label(self):
        return self.label

    def get_type(self):
        return self.wtype

    def get_readonly(self):
        return self.readonly

    def get_value(self):
        if self.value_error is not None:
            raise self.value_error
        return self.value

    def set_value(self, value):
        if self.readonly:
            raise gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED,
                                  f"widget {self.name!r} is read-only")
        self.value = value

    def get_children(self):
        return list(self.children)

    def get_child_by_name(self, name):
        found = self.find(name)
        if found is None:
            raise gp.GPhoto2Error(gp.GP_ERROR_BAD_PARAMETERS,
                                  f"no widget named {name!r}")
        return found

    def count_choices(self):
        return len(self.choices)

    def get_choice(self, index):
        return self.choices[index]

    def get_range(self):
        return self.rng

    def find(self, name):
        if self.name == name:
            return self
        for child in self.children:
            hit = child.find(name)
            if hit is not None:
                return hit
        return None

    def copy(self):
        clone = FakeWidget(self.name, self.wtype, self.value, self.label,
                           self.choices, self.rng, self.readonly, (),
                           self.value_error)
        clone.children = [c.copy() for c in self.children]
        return clone

    def apply_values_from(self, other):
        mine = self.find(other.name)
        if mine is not None and not other.children:
            mine.value = other.value
        for child in other.children:
            self.apply_values_from(child)


class FakeCameraFile:
    def __init__(self, data):
        self.data = bytearray(data)

    def get_data_and_size(self):
        return self.data

    def save(self, target):
        with open(target, "wb") as fh:
            fh.write(self.data)


class Abilities:
    def __init__(self, model):
        self.model = model


class FakeDevice:
    def __init__(self, config=None, clock=None, model="Sony Alpha-A7 IV (PC Control)"):
        self.config = config if config is not None else default_config()
        self.clock = clock
        self.model = model
        self.calls = []
        self.events = []
        self.capture_results = []
        self.preview_frames = []
        self.hook = None
        self.exit_count = 0
        self.abilities_error = None
        self.init_error = None
        self.event_poll_cost = 0.01
        self.image_bytes = b"\xff\xd8\xff\xe0raw-image-data"

    def _record(self, method, *args):
        self.calls.append((method, *args))
        if self.hook is not None:
            self.hook(method, *args)

    def calls_named(self, method):
        return [c for c in self.calls if c[0] == method]

    def value_of(self, name):
        return self.config.get_child_by_name(name).value

    def init(self):
        self._record("init")
        if self.init_error is not None:
            raise self.init_error

    def get_config(self):
        self._record("get_config")
        return self.config.copy()

    def set_config(self, cfg):
        self._record("set_config")
        self.config.apply_values_from(cfg)

    def get_single_config(self, name):
        self._record("get_single_config", name)
        leaf = self.config.get_child_by_name(name).copy()
        leaf.children = []
        return leaf

    def set_single_config(self, name, widget):
        self._record("set_single_config", name, widget.value)
        self.config.get_child_by_name(name).value = widget.value

    def capture(self, capture_type):
        self._record("capture", capture_type)
        return _next(self.capture_results, gp.CameraFilePath())

    def capture_preview(self):
        self._record("capture_preview")
        return FakeCameraFile(_next(self.preview_frames, b"\xff\xd8jpeg-frame"))

    def file_get(self, folder, name, file_type):
        self._record("file_get", folder, name, file_type)
        return FakeCameraFile(self.image_bytes)

    def wait_for_event(self, timeout_ms):
        self._record("wait_for_event", timeout_ms)
        if self.events:
            event = _next(self.events, None)
            if self.clock is not None:
                self.clock.advance(self.event_poll_cost)
            return event
        if self.clock is not None:
            self.clock.advance(timeout_ms / 1000.0)
        return (gp.GP_EVENT_TIMEOUT, None)

    def get_abilities(self):
        self._record("get_abilities")
        if self.abilities_error is not None:
            raise self.abilities_error
        return Abilities(self.model)

    def exit(self):
        self._record("exit")
        self.exit_count += 1


class FakeConnectedCamera:
    def __init__(self):
        self.model = "Sony Alpha-A7 IV (PC Control)"
        self.recording = False
        self.closed = False
        self.calls = []
        self.errors = {}
        self.results = {}

    def _do(self, name, *args):
        self.calls.append((name, *args))
        if name in self.errors:
            raise self.errors[name]
        if name in self.results:
            return self.results[name]
        return None

    def capture(self):
        return self._do("capture") or "captures/1774000000_DSC00001.ARW"

    def bulb(self, seconds):
        return self._do("bulb", seconds) or "captures/1774000000_DSC00002.ARW"

    def preview(self):
        return self._do("preview") or b"\xff\xd8jpeg"

    def set_recording(self, on):
        self._do("set_recording", on)
        self.recording = bool(on)
        return self.recording

    def autofocus(self):
        return self._do("autofocus") or "AF-A"

    def manual_focus(self, steps):
        return self._do("manual_focus", steps) or "Manual"

    def set_af_point(self, x, y):
        return self._do("set_af_point", x, y)

    def telemetry(self):
        result = self._do("telemetry")
        return result if result is not None else [
            {"name": "batterylevel", "label": "Battery Level", "value": "87%"}]

    def list_settings(self):
        result = self._do("list_settings")
        return result if result is not None else [
            {"name": "iso", "label": "ISO Speed", "type": "choice",
             "value": "400", "choices": ["100", "400", "800"]}]

    def set_setting(self, name, value):
        return self._do("set_setting", name, value)

    def close(self):
        self.calls.append(("close",))
        self.closed = True

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


def _next(queue, default):
    if not queue:
        return default
    item = queue.pop(0)
    if isinstance(item, Exception):
        raise item
    return item


def default_config():
    return FakeWidget("main", gp.GP_WIDGET_WINDOW, children=[
        FakeWidget("imgsettings", gp.GP_WIDGET_SECTION, children=[
            FakeWidget("iso", gp.GP_WIDGET_RADIO, value="400",
                       label="ISO Speed", choices=("100", "400", "800")),
            FakeWidget("whitebalance", gp.GP_WIDGET_MENU, value="Automatic",
                       label="WhiteBalance", choices=("Automatic", "Daylight")),
            FakeWidget("imagequality", gp.GP_WIDGET_RADIO, value="RAW",
                       choices=("RAW", "JPEG"), readonly=True),
        ]),
        FakeWidget("capturesettings", gp.GP_WIDGET_SECTION, children=[
            FakeWidget("f-number", gp.GP_WIDGET_RADIO, value="f/2.8",
                       label="F-Number", choices=("f/2.8", "f/4")),
            FakeWidget("burstnumber", gp.GP_WIDGET_RANGE, value=1.0,
                       label="Burst Number", rng=(1.0, 10.0, 1.0)),
            FakeWidget("focusmode", gp.GP_WIDGET_RADIO, value="Manual",
                       label="Focus Mode",
                       choices=("Automatic", "AF-A", "AF-C", "AF-S", "DMF", "Manual")),
            FakeWidget("liveviewsize", gp.GP_WIDGET_BUTTON, value=None,
                       label="Live View Size"),
        ]),
        FakeWidget("settings", gp.GP_WIDGET_SECTION, children=[
            FakeWidget("datetime-group", gp.GP_WIDGET_SECTION, children=[
                FakeWidget("datetime", gp.GP_WIDGET_TEXT, value="2026-07-25T12:00:00",
                           label="Camera Date and Time"),
            ]),
        ]),
        FakeWidget("actions", gp.GP_WIDGET_SECTION, children=[
            FakeWidget("autofocus", gp.GP_WIDGET_TOGGLE, value=2),
            FakeWidget("autofocusdrive", gp.GP_WIDGET_TOGGLE, value=0),
            FakeWidget("manualfocus", gp.GP_WIDGET_RANGE, value=0.0, rng=(-7.0, 7.0, 1.0)),
            FakeWidget("manualfocusdrive", gp.GP_WIDGET_RANGE, value=0.0, rng=(-7.0, 7.0, 1.0)),
            FakeWidget("bulb", gp.GP_WIDGET_TOGGLE, value=0),
            FakeWidget("movie", gp.GP_WIDGET_TOGGLE, value=0),
            FakeWidget("changeafarea", gp.GP_WIDGET_TEXT, value=""),
        ]),
        FakeWidget("status", gp.GP_WIDGET_SECTION, children=[
            FakeWidget("batterylevel", gp.GP_WIDGET_TEXT, value="87%",
                       label="Battery Level"),
            FakeWidget("lensname", gp.GP_WIDGET_TEXT, value="FE 24-70mm F2.8 GM II",
                       label="Lens Name"),
            FakeWidget("serialnumber", gp.GP_WIDGET_TEXT, value="unreadable",
                       label="Serial Number",
                       value_error=gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED)),
        ]),
    ])
