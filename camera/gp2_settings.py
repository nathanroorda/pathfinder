"""Camera settings: read and write the gphoto2 config tree.

A "setting" is a value you get/set (ISO, aperture, white balance...). This is
strictly separate from actions/ commands — only settable value widgets from
whitelisted sections are exposed, never action triggers, which keeps the panel
safe (setting an action widget can crash the driver).
"""
import gphoto2 as gp

# gphoto2 widget types -> the simple control kind the UI renders.
# Add a mapping here to support a new control type.
_KIND = {
    gp.GP_WIDGET_RADIO: "choice",
    gp.GP_WIDGET_MENU: "choice",
    gp.GP_WIDGET_TOGGLE: "toggle",
    gp.GP_WIDGET_RANGE: "range",
    gp.GP_WIDGET_TEXT: "text",
}

# Only expose widgets under these top-level config sections. /main/actions holds
# command triggers (autofocus, capture, bulb, movie) that look like settings but
# RUN when set; /main/status is read-only info. Add a section name to expose more.
INCLUDE_SECTIONS = {"imgsettings", "capturesettings", "settings"}

# Individual setting names to hide from the UI.
EXCLUDE = set()


class SettingsMixin:
    """Adds settings read/write to a camera that provides self._cam / self._lock."""

    def list_settings(self):
        """Every writable camera setting, as UI-ready descriptors."""
        with self._lock:
            return [self._describe(w)
                    for w in self._walk_sections(self._cam.get_config())
                    if self._is_setting(w)]

    def set_setting(self, name, value):
        """Set one setting; value is coerced to the widget's native type."""
        with self._lock:
            cfg = self._cam.get_config()
            widget = cfg.get_child_by_name(name)
            widget.set_value(_coerce(widget.get_type(), value))
            self._cam.set_config(cfg)

    def _walk_sections(self, root):
        for section in root.get_children():
            if section.get_name() in INCLUDE_SECTIONS:
                yield from self._walk(section)

    def _walk(self, widget):
        for child in widget.get_children():
            if child.get_type() in (gp.GP_WIDGET_WINDOW, gp.GP_WIDGET_SECTION):
                yield from self._walk(child)
            else:
                yield child

    def _is_setting(self, widget):
        return (widget.get_type() in _KIND
                and not widget.get_readonly()
                and widget.get_name() not in EXCLUDE)

    def _describe(self, widget):
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