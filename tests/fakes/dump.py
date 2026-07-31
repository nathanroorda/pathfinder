import os
import unittest

import gphoto2 as gp

from .fake_camera import FakeWidget

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.pardir, "fixtures")

A7IV_MODEL = "Sony Alpha-A7 IV (PC Control)"

_TYPES = {
    "TEXT": gp.GP_WIDGET_TEXT,
    "RANGE": gp.GP_WIDGET_RANGE,
    "TOGGLE": gp.GP_WIDGET_TOGGLE,
    "RADIO": gp.GP_WIDGET_RADIO,
    "MENU": gp.GP_WIDGET_MENU,
    "BUTTON": gp.GP_WIDGET_BUTTON,
    "DATE": gp.GP_WIDGET_DATE,
}


def _fields(line):
    key, _, value = line.partition(":")
    return key, value[1:] if value.startswith(" ") else value


def blocks(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("/main/"):
            continue
        if i + 1 >= len(lines) or not lines[i + 1].startswith("Label:"):
            continue
        widget = {"path": line, "choices": []}
        for entry in lines[i + 1:]:
            if entry == "END":
                break
            key, value = _fields(entry)
            if key == "Choice":
                widget["choices"].append(value.partition(" ")[2])
            else:
                widget[key.lower()] = value
        yield widget


def _value(wtype, raw):
    if wtype == gp.GP_WIDGET_TOGGLE:
        return int(raw) if raw else 0
    if wtype == gp.GP_WIDGET_RANGE:
        return float(raw) if raw else 0.0
    return raw


def _widget(fields):
    wtype = _TYPES[fields["type"]]
    name = fields["path"].rsplit("/", 1)[1]
    rng = None
    if wtype == gp.GP_WIDGET_RANGE:
        rng = (float(fields["bottom"]), float(fields["top"]),
               float(fields["step"]))
    return FakeWidget(
        name, wtype,
        value=_value(wtype, fields.get("current", "")),
        label=fields.get("label", name),
        choices=tuple(fields["choices"]),
        rng=rng,
        readonly=fields.get("readonly") == "1",
    )


def build_config(text):
    sections = {}
    for fields in blocks(text):
        sections.setdefault(fields["path"].split("/")[2], []).append(
            _widget(fields))
    if not sections:
        raise ValueError("no widget blocks found — is this --list-all-config output?")
    return FakeWidget("main", gp.GP_WIDGET_WINDOW, children=[
        FakeWidget(name, gp.GP_WIDGET_SECTION, children=children)
        for name, children in sections.items()
    ])


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def model_of(text):
    for line in text.splitlines():
        if line.startswith("Abilities for camera"):
            return line.partition(":")[2].strip() or None
        if line.startswith("/main/"):
            break
    return None


class Fixture:
    def __init__(self, path, text):
        self.path = path
        self.name = os.path.basename(path)
        self.model = model_of(text)
        self._config = build_config(text)

    def config(self):
        return self._config.copy()

    def __repr__(self):
        return f"Fixture({self.name!r}, model={self.model!r})"


_loaded = None


def fixtures():
    global _loaded
    if _loaded is None:
        found = []
        for name in sorted(os.listdir(FIXTURES)) if os.path.isdir(FIXTURES) else []:
            if not name.endswith(".txt"):
                continue
            path = os.path.join(FIXTURES, name)
            text = read(path)
            if model_of(text) is None:
                continue
            found.append(Fixture(path, text))
        _loaded = found
    return list(_loaded)


def fixture_for(model):
    for fixture in fixtures():
        if fixture.model == model:
            return fixture
    return None


def a7iv_config():
    fixture = fixture_for(A7IV_MODEL)
    if fixture is None:
        raise unittest.SkipTest(f"no dump for {A7IV_MODEL} in {FIXTURES}")
    return fixture.config()
