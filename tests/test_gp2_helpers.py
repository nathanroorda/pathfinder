import types
import unittest
from unittest import mock

from tests import support

import gphoto2 as gp
from camera import gp2, sony
from tests.fakes.fake_camera import FakeWidget


def widget(wtype, rng=(-7.0, 7.0, 1.0), name="w"):
    return FakeWidget(name, wtype, rng=rng)


class Coerce(unittest.TestCase):
    def test_range_becomes_float(self):
        for value in (3, 3.0, "3", "3.5", True):
            with self.subTest(value=value):
                self.assertIsInstance(
                    gp2._coerce(widget(gp.GP_WIDGET_RANGE), value), float)
        self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_RANGE), "-7"), -7.0)

    def test_toggle_becomes_int(self):
        toggle = widget(gp.GP_WIDGET_TOGGLE)
        self.assertEqual(gp2._coerce(toggle, True), 1)
        self.assertEqual(gp2._coerce(toggle, False), 0)
        self.assertEqual(gp2._coerce(toggle, "1"), 1)
        self.assertIsInstance(gp2._coerce(toggle, 1.0), int)

    def test_everything_else_becomes_str(self):
        for wtype in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU, gp.GP_WIDGET_TEXT):
            with self.subTest(wtype=wtype):
                self.assertEqual(gp2._coerce(widget(wtype), 400), "400")
                self.assertEqual(gp2._coerce(widget(wtype), "f/2.8"), "f/2.8")

    def test_uncoercible_value_raises_rather_than_reaching_usb(self):
        with self.assertRaises(ValueError):
            gp2._coerce(widget(gp.GP_WIDGET_RANGE), "not-a-number")


class RangeClamping(unittest.TestCase):
    def test_values_inside_the_range_pass_through(self):
        for value in (-7, -3.5, 0, 7):
            with self.subTest(value=value):
                self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_RANGE), value), float(value))

    def test_values_outside_the_range_are_clamped_to_it(self):
        self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_RANGE), 100000), 7.0)
        self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_RANGE), -100000), -7.0)

    def test_infinities_are_clamped_rather_than_reaching_the_motor(self):
        self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_RANGE), float("inf")), 7.0)
        self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_RANGE), float("-inf")), -7.0)

    def test_nan_is_rejected_rather_than_collapsing_to_a_bound(self):
        with self.assertRaises(ValueError):
            gp2._coerce(widget(gp.GP_WIDGET_RANGE), float("nan"))

    def test_clamping_is_logged(self):
        with self.assertLogs("camera.gp2", level="WARNING") as captured:
            gp2._coerce(widget(gp.GP_WIDGET_RANGE, name="manualfocus"), 100000)
        self.assertIn("manualfocus", "\n".join(captured.output))

    def test_each_widget_is_clamped_to_its_own_declared_range(self):
        self.assertEqual(
            gp2._coerce(widget(gp.GP_WIDGET_RANGE, rng=(1.0, 10.0, 1.0)), 99), 10.0)
        self.assertEqual(
            gp2._coerce(widget(gp.GP_WIDGET_RANGE, rng=(0.0, 255.0, 1.0)), 99), 99.0)

    def test_non_range_widgets_are_not_clamped(self):
        self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_TEXT), 100000), "100000")
        self.assertEqual(gp2._coerce(widget(gp.GP_WIDGET_TOGGLE), 100000), 100000)


class Scale(unittest.TestCase):
    def test_maps_the_unit_interval_onto_the_grid(self):
        self.assertEqual(gp2._scale(0.0, 640), 0)
        self.assertEqual(gp2._scale(1.0, 640), 640)
        self.assertEqual(gp2._scale(0.5, 640), 320)
        self.assertEqual(gp2._scale(0.5, 480), 240)

    def test_clamps_out_of_range_input(self):
        self.assertEqual(gp2._scale(-0.4, 640), 0)
        self.assertEqual(gp2._scale(-1e9, 640), 0)
        self.assertEqual(gp2._scale(1.4, 640), 640)
        self.assertEqual(gp2._scale(float("inf"), 640), 640)

    def test_accepts_string_input_and_returns_int(self):
        self.assertEqual(gp2._scale("0.25", 640), 160)
        self.assertIsInstance(gp2._scale(0.3, 640), int)

    def test_nan_collapses_to_zero_rather_than_reaching_the_body(self):
        self.assertEqual(gp2._scale(float("nan"), 640), 0)


class DisconnectClassification(unittest.TestCase):
    def test_transport_codes_are_disconnects(self):
        for code in (-7, -31, -34, -35, -52, -53):
            with self.subTest(code=code):
                self.assertTrue(gp2.is_disconnect_error(gp.GPhoto2Error(code)))

    def test_usb_find_52_is_a_disconnect(self):
        self.assertTrue(
            gp2.is_disconnect_error(gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)))

    def test_our_own_closed_connection_marker_is_a_disconnect(self):
        self.assertTrue(gp2.is_disconnect_error(gp2.CameraDisconnected("closed")))

    def test_logical_errors_are_not_disconnects(self):
        for code in (gp.GP_ERROR, gp.GP_ERROR_BAD_PARAMETERS,
                     gp.GP_ERROR_NOT_SUPPORTED, gp.GP_ERROR_TIMEOUT):
            with self.subTest(code=code):
                self.assertFalse(gp2.is_disconnect_error(gp.GPhoto2Error(code)))

    def test_non_gphoto_exceptions_are_not_disconnects(self):
        for exc in (RuntimeError("busy"), ValueError("bad"), OSError("io")):
            with self.subTest(exc=exc):
                self.assertFalse(gp2.is_disconnect_error(exc))


class QuirkResolution(unittest.TestCase):
    def test_sony_model_resolves_to_the_sony_table(self):
        self.assertEqual(
            gp2._quirks_for("Sony Alpha-A7 IV (PC Control)")["af_widget"], "autofocus")

    def test_unmatched_model_falls_back_to_generic_defaults(self):
        self.assertEqual(gp2._quirks_for("Canon EOS R5"), gp2.DEFAULT_QUIRKS)

    def test_unmatched_model_is_logged_loudly(self):
        with self.assertLogs("camera.gp2", level="WARNING") as captured:
            gp2._quirks_for("Canon EOS R5")
        self.assertIn("Canon EOS R5", "\n".join(captured.output))

    def test_no_vendor_table_names_a_quirk_that_does_not_exist(self):
        for vendor in gp2.VENDORS:
            with self.subTest(vendor=vendor.__name__):
                self.assertLessEqual(set(vendor.GENERAL), set(gp2.DEFAULT_QUIRKS))

    def test_model_overrides_introduce_no_unknown_keys(self):
        for override in sony.MODELS.values():
            self.assertLessEqual(set(override), set(gp2.DEFAULT_QUIRKS))

    def test_a_vendor_table_need_not_repeat_every_default(self):
        partial_vendor = types.SimpleNamespace(
            __name__="acme",
            quirks=lambda model: {"shot_gap": 0.5} if "acme" in model.lower() else None)

        with mock.patch.object(gp2, "VENDORS", [partial_vendor]):
            quirks = gp2._quirks_for("ACME Snapmaster")

        self.assertEqual(set(quirks), set(gp2.DEFAULT_QUIRKS))
        self.assertEqual(quirks["shot_gap"], 0.5)
        self.assertEqual(quirks["movie_widget"], gp2.DEFAULT_QUIRKS["movie_widget"])

    def test_a_vendor_naming_a_quirk_that_does_not_exist_is_refused(self):
        typo_vendor = types.SimpleNamespace(
            __name__="acme", quirks=lambda model: {"af_widgets": "autofocus"})

        with mock.patch.object(gp2, "VENDORS", [typo_vendor]):
            with self.assertRaises(ValueError) as caught:
                gp2._quirks_for("ACME Snapmaster")

        self.assertIn("af_widgets", str(caught.exception))

    def test_layering_does_not_mutate_the_shared_default_table(self):
        before = dict(gp2.DEFAULT_QUIRKS)
        partial_vendor = types.SimpleNamespace(
            __name__="acme", quirks=lambda model: {"shot_gap": 99.0})

        with mock.patch.object(gp2, "VENDORS", [partial_vendor]):
            gp2._quirks_for("ACME Snapmaster")

        self.assertEqual(gp2.DEFAULT_QUIRKS, before)

    def test_sony_no_longer_repeats_a_default_it_agrees_with(self):
        self.assertNotIn("movie_widget", sony.GENERAL)
        self.assertEqual(
            gp2._quirks_for("Sony Alpha-A7 IV (PC Control)")["movie_widget"],
            "movie")

    def test_first_matching_vendor_wins(self):
        other = mock.Mock()
        other.quirks.return_value = {"shot_gap": 9.0}
        with mock.patch.object(gp2, "VENDORS", [other, sony]):
            self.assertEqual(gp2._quirks_for("Sony Alpha-A7 IV")["shot_gap"], 9.0)


class WidgetWalk(unittest.TestCase):
    def _tree(self):
        return FakeWidget("main", gp.GP_WIDGET_WINDOW, children=[
            FakeWidget("iso", gp.GP_WIDGET_RADIO, value="400", choices=("100", "400")),
            FakeWidget("group", gp.GP_WIDGET_SECTION, children=[
                FakeWidget("deep", gp.GP_WIDGET_SECTION, children=[
                    FakeWidget("nested", gp.GP_WIDGET_TEXT, value="x"),
                ]),
                FakeWidget("shallow", gp.GP_WIDGET_TOGGLE, value=0),
            ]),
        ])

    def test_returns_leaves_from_every_depth(self):
        names = [w.get_name() for w in gp2._walk(self._tree())]
        self.assertEqual(names, ["iso", "nested", "shallow"])

    def test_containers_are_not_themselves_returned(self):
        names = [w.get_name() for w in gp2._walk(self._tree())]
        self.assertNotIn("group", names)
        self.assertNotIn("deep", names)

    def test_empty_tree_is_not_an_error(self):
        self.assertEqual(gp2._walk(FakeWidget("main", gp.GP_WIDGET_WINDOW)), [])


class Describe(unittest.TestCase):
    def test_choice_carries_its_options(self):
        widget = FakeWidget("iso", gp.GP_WIDGET_RADIO, value="400",
                            label="ISO Speed", choices=("100", "400", "800"))
        self.assertEqual(gp2._describe(widget), {
            "name": "iso", "label": "ISO Speed", "type": "choice",
            "value": "400", "choices": ["100", "400", "800"], "readonly": False,
        })

    def test_menu_is_also_a_choice(self):
        widget = FakeWidget("wb", gp.GP_WIDGET_MENU, value="Auto", choices=("Auto",))
        self.assertEqual(gp2._describe(widget)["type"], "choice")

    def test_range_carries_min_max_step(self):
        widget = FakeWidget("burst", gp.GP_WIDGET_RANGE, value=1.0, rng=(1.0, 10.0, 1.0))
        described = gp2._describe(widget)
        self.assertEqual(described["type"], "range")
        self.assertEqual((described["min"], described["max"], described["step"]),
                         (1.0, 10.0, 1.0))
        self.assertNotIn("choices", described)

    def test_toggle_and_text_carry_only_the_common_keys(self):
        for wtype, kind in ((gp.GP_WIDGET_TOGGLE, "toggle"),
                            (gp.GP_WIDGET_TEXT, "text")):
            with self.subTest(kind=kind):
                described = gp2._describe(FakeWidget("w", wtype, value=1))
                self.assertEqual(described["type"], kind)
                self.assertEqual(set(described),
                                 {"name", "label", "type", "value", "readonly"})

    def test_readonly_is_reported_as_a_bool_the_browser_can_test(self):
        widget = FakeWidget("shutterspeed", gp.GP_WIDGET_RADIO, value="1/30",
                            choices=("1/30",), readonly=True)
        self.assertIs(gp2._describe(widget)["readonly"], True)

    def test_a_widget_whose_value_cannot_be_read_still_describes(self):
        widget = FakeWidget("broken", gp.GP_WIDGET_TEXT, value="x",
                            value_error=gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED))
        self.assertIsNone(gp2._describe(widget)["value"])

    def test_every_kind_the_browser_renders_is_producible(self):
        self.assertEqual(set(gp2._KIND.values()), {"choice", "toggle", "range", "text"})


class WorthShowing(unittest.TestCase):
    def test_a_writable_widget_of_any_kind_is_shown(self):
        for wtype in gp2._KIND:
            with self.subTest(wtype=wtype):
                widget = FakeWidget("w", wtype, value=1, choices=("a",),
                                    rng=(0.0, 1.0, 1.0))
                self.assertTrue(gp2._worth_showing(widget))

    def test_a_readonly_choice_is_shown_because_it_has_a_value_and_options(self):
        widget = FakeWidget("f-number", gp.GP_WIDGET_RADIO, value="f/3.5",
                            choices=("f/3.5", "f/4"), readonly=True)
        self.assertTrue(gp2._worth_showing(widget))

    def test_a_readonly_range_is_hidden(self):
        widget = FakeWidget("colortemperature", gp.GP_WIDGET_RANGE, value=0.0,
                            rng=(2500.0, 9900.0, 100.0), readonly=True)
        self.assertFalse(gp2._worth_showing(widget))

    def test_a_readonly_choice_offering_nothing_is_hidden(self):
        widget = FakeWidget("empty", gp.GP_WIDGET_RADIO, value="",
                            choices=(), readonly=True)
        self.assertFalse(gp2._worth_showing(widget))

    def test_a_readonly_toggle_or_text_is_hidden(self):
        for wtype in (gp.GP_WIDGET_TOGGLE, gp.GP_WIDGET_TEXT):
            with self.subTest(wtype=wtype):
                widget = FakeWidget("w", wtype, value=1, readonly=True)
                self.assertFalse(gp2._worth_showing(widget))


class DescribeStatus(unittest.TestCase):
    def test_reports_name_label_value(self):
        widget = FakeWidget("batterylevel", gp.GP_WIDGET_TEXT, value="87%",
                            label="Battery Level")
        self.assertEqual(gp2._describe_status(widget), {
            "name": "batterylevel", "label": "Battery Level", "value": "87%"})

    def test_unreadable_widget_degrades_to_none_instead_of_failing_the_request(self):
        widget = FakeWidget("serialnumber", gp.GP_WIDGET_TEXT, value="x",
                            value_error=gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED))
        self.assertIsNone(gp2._describe_status(widget)["value"])

    def test_non_gphoto_errors_still_propagate(self):
        widget = FakeWidget("boom", gp.GP_WIDGET_TEXT, value_error=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            gp2._describe_status(widget)


if __name__ == "__main__":
    unittest.main()
