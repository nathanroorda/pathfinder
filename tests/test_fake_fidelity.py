import os
import re
import unittest

from tests import support
from tests.fakes import dump, fake_gphoto2
from tests.fakes.fake_camera import (FakeCameraFile, FakeConnectedCamera, FakeDevice, FakeWidget)

import gphoto2 as gp

from camera import gp2, sony
from camera.gp2 import Gphoto2Camera


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


GP2_TEXT = read(os.path.join(support.REPO_ROOT, "camera", "gp2.py"))
APP_TEXT = read(os.path.join(support.REPO_ROOT, "app", "app.py"))


@unittest.skipIf(support.REAL_GPHOTO2 is None,
                 "the real gphoto2 binding is not installed here")
class MatchesTheRealBinding(unittest.TestCase):
    def constants(self):
        return {name: value for name, value in vars(fake_gphoto2).items()
                if name.startswith("GP_")}

    def test_every_faked_constant_exists_upstream(self):
        missing = [name for name in self.constants()
                   if not hasattr(support.REAL_GPHOTO2, name)]
        self.assertEqual(missing, [])

    def test_every_faked_constant_has_the_real_value(self):
        for name, value in self.constants().items():
            with self.subTest(constant=name):
                self.assertEqual(value, getattr(support.REAL_GPHOTO2, name))

    def test_the_disconnect_codes_the_app_relies_on_all_exist(self):
        for name in ("GP_ERROR_IO", "GP_ERROR_IO_INIT", "GP_ERROR_IO_READ",
                     "GP_ERROR_IO_WRITE", "GP_ERROR_IO_USB_FIND",
                     "GP_ERROR_IO_USB_CLAIM"):
            with self.subTest(constant=name):
                self.assertTrue(hasattr(support.REAL_GPHOTO2, name))

    def test_the_error_type_carries_a_code(self):
        error = support.REAL_GPHOTO2.GPhoto2Error(
            support.REAL_GPHOTO2.GP_ERROR_IO_USB_FIND)
        self.assertEqual(error.code, -52)

    def test_the_types_the_fake_stands_in_for_exist(self):
        for name in ("Camera", "CameraFilePath", "GPhoto2Error"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(support.REAL_GPHOTO2, name))


class CoversWhatTheCodeCalls(unittest.TestCase):
    def test_the_fake_device_implements_every_camera_call(self):
        called = set(re.findall(r"(?:self\._cam|\bcam)\.(\w+)\(", GP2_TEXT))

        self.assertIn("capture", called)
        missing = {name for name in called if not hasattr(FakeDevice, name)}
        self.assertEqual(missing, set(), f"FakeDevice is missing {missing}")

    def test_the_fake_widget_implements_every_widget_call(self):
        called = set(re.findall(
            r"\b(?:widget|cfg|config|child|section)\.(\w+)\(", GP2_TEXT))

        self.assertIn("get_value", called)
        missing = {name for name in called if not hasattr(FakeWidget, name)}
        self.assertEqual(missing, set(), f"FakeWidget is missing {missing}")

    def test_the_fake_file_implements_every_camera_file_call(self):
        for name in ("get_data_and_size", "save"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(FakeCameraFile, name))

    def test_the_fake_exposes_every_gp_constant_the_code_reads(self):
        constants = set(re.findall(r"\bgp\.(GP_\w+)\b", GP2_TEXT))

        self.assertGreater(len(constants), 5)
        missing = {name for name in constants if not hasattr(fake_gphoto2, name)}
        self.assertEqual(missing, set(), f"fake_gphoto2 is missing {missing}")


class TheFixtureDirectoryIsWellFormed(unittest.TestCase):
    def txt_files(self):
        if not os.path.isdir(dump.FIXTURES):
            return []
        return sorted(n for n in os.listdir(dump.FIXTURES) if n.endswith(".txt"))

    def test_no_two_dumps_describe_the_same_body(self):
        seen = {}
        for fixture in dump.fixtures():
            seen.setdefault(fixture.model, []).append(fixture.name)

        duplicated = {model: names for model, names in seen.items()
                      if len(names) > 1}
        self.assertEqual(duplicated, {},
                         "two dumps for one body — delete the stale one")

    def test_every_txt_file_is_a_usable_dump(self):
        loaded = {fixture.name for fixture in dump.fixtures()}
        unparseable = [name for name in self.txt_files() if name not in loaded]

        self.assertEqual(unparseable, [],
                         "not valid gphoto2 dumps — re-run tools/camera-dump.sh")


@unittest.skipUnless(dump.fixtures(),
                     "no hardware dumps in tests/fixtures/ — see its README")
class EveryDumpedBodyMatchesItsQuirks(unittest.TestCase):
    WIDGET_QUIRKS = ("movie_widget", "af_widget", "manual_focus_widget",
                     "focus_mode_widget", "bulb_widget", "af_area_widget",
                     "magnifier_widget")

    def vendor_matched(self):
        for fixture in dump.fixtures():
            quirks = gp2._quirks_for(fixture.model)
            if quirks is not gp2.DEFAULT_QUIRKS:
                yield fixture, quirks

    def test_every_named_widget_exists_on_the_body(self):
        checked = 0
        for fixture, quirks in self.vendor_matched():
            config = fixture.config()
            for quirk in self.WIDGET_QUIRKS:
                name = quirks[quirk]
                if name is None:
                    continue
                with self.subTest(body=fixture.model, quirk=quirk, widget=name):
                    self.assertIsNotNone(
                        config.find(name),
                        f"{quirk}={name!r} is not a widget on {fixture.model} "
                        f"({fixture.name})")
                    checked += 1
        self.assertGreater(checked, 0, "no vendor-matched dump to check against")

    def test_focus_mode_targets_are_real_choices_on_every_body(self):
        for fixture, quirks in self.vendor_matched():
            widget = fixture.config().find(quirks["focus_mode_widget"] or "")
            if widget is None:
                continue
            choices = {widget.get_choice(i) for i in range(widget.count_choices())}
            for quirk in ("af_target_mode", "mf_target_mode"):
                with self.subTest(body=fixture.model, quirk=quirk):
                    self.assertIn(quirks[quirk], choices)

    def test_unclaimed_bodies_are_reported(self):
        unclaimed = [f.model for f in dump.fixtures()
                     if gp2._quirks_for(f.model) is gp2.DEFAULT_QUIRKS]
        if unclaimed:
            self.skipTest(f"no vendor quirks for: {', '.join(unclaimed)} — "
                          f"add a module to camera/gp2.py VENDORS")


@unittest.skipUnless(dump.fixture_for(dump.A7IV_MODEL),
                     f"no dump for {dump.A7IV_MODEL} in tests/fixtures/")
class TheQuirksMatchTheHardware(unittest.TestCase):
    def setUp(self):
        self.config = dump.a7iv_config()
        self.quirks = sony.quirks(dump.A7IV_MODEL)

    def test_the_fixture_is_the_body_the_quirks_target(self):
        self.assertIsNotNone(self.quirks, "sony.quirks() did not match the a7 IV")
        self.assertIsNotNone(self.config.find("iso"))

    def choices_of(self, name):
        widget = self.config.find(name)
        return {widget.get_choice(i) for i in range(widget.count_choices())}

    def test_the_focus_mode_targets_are_real_choices(self):
        choices = self.choices_of(self.quirks["focus_mode_widget"])
        for quirk in ("af_target_mode", "mf_target_mode"):
            with self.subTest(quirk=quirk):
                self.assertIn(self.quirks[quirk], choices)

    def test_the_acceptable_focus_modes_overlap_the_body(self):
        choices = self.choices_of(self.quirks["focus_mode_widget"])
        for quirk in ("af_modes", "mf_modes"):
            with self.subTest(quirk=quirk):
                self.assertTrue(set(self.quirks[quirk]) & choices,
                                f"{quirk} names no mode this body offers")

    def test_the_manual_focus_steps_fit_the_widget_range(self):
        widget = self.config.find(self.quirks["manual_focus_widget"])
        self.assertEqual(widget.get_type(), gp.GP_WIDGET_RANGE)
        self.assertEqual(widget.get_range(), (-7.0, 7.0, 1.0))

    def test_the_af_and_bulb_drives_take_the_values_the_quirks_send(self):
        for quirk in ("af_widget", "bulb_widget"):
            with self.subTest(quirk=quirk):
                widget = self.config.find(self.quirks[quirk])
                self.assertEqual(widget.get_type(), gp.GP_WIDGET_TOGGLE)

    def test_the_magnifier_rest_value_is_a_real_choice(self):
        self.assertIn(self.quirks["magnifier_off"],
                      self.choices_of(self.quirks["magnifier_widget"]))

    def test_the_magnifier_is_a_writable_radio(self):
        widget = self.config.find(self.quirks["magnifier_widget"])
        self.assertEqual(widget.get_type(), gp.GP_WIDGET_RADIO)
        self.assertFalse(widget.get_readonly())

    def test_the_af_area_widget_takes_the_x_y_string_the_driver_sends(self):
        widget = self.config.find(self.quirks["af_area_widget"])
        self.assertEqual(widget.get_type(), gp.GP_WIDGET_TEXT)
        self.assertFalse(widget.get_readonly())

    def test_the_canon_name_is_gone_for_good(self):
        self.assertIsNone(self.config.find("changeafarea"))
        self.assertNotIn("changeafarea", sony.GENERAL.values())


@unittest.skipUnless(dump.fixture_for(dump.A7IV_MODEL),
                     f"no dump for {dump.A7IV_MODEL} in tests/fixtures/")
class TheFixtureIsARealDump(unittest.TestCase):
    def setUp(self):
        self.config = dump.a7iv_config()

    def sections(self):
        return {s.get_name(): s.get_children()
                for s in self.config.get_children()}

    def test_it_carries_the_whole_tree_the_body_published(self):
        counts = {name: len(kids) for name, kids in self.sections().items()}
        self.assertEqual(counts, {"actions": 8, "settings": 2, "status": 7,
                                  "imgsettings": 4, "capturesettings": 22,
                                  "other": 347})

    def test_the_raw_ptp_section_is_present_so_exclusion_is_worth_testing(self):
        listed = {w.get_name()
                  for w in gp2._settable_widgets(self.config)}
        raw = {w.get_name() for w in self.sections()["other"]}

        self.assertEqual(listed & raw, set())

    def test_the_settable_surface_is_the_twenty_widgets_measured_on_the_body(self):
        listed = [w.get_name() for w in gp2._settable_widgets(self.config)]
        self.assertEqual(len(listed), 20)
        self.assertNotIn("shutterspeed", listed)  # ro on this body
        self.assertNotIn("f-number", listed)      # ro on this body

    def test_parsing_a_dump_with_no_widget_blocks_is_an_error(self):
        with self.assertRaises(ValueError):
            dump.build_config("Camera summary:\nnothing here\n")


class TheCameraContract(unittest.TestCase):
    # Test-only affordances the double adds for failure injection.
    DOUBLE_ONLY = {"calls", "called", "errors", "results", "closed"}

    def setUp(self):
        self.real = Gphoto2Camera(FakeDevice(), "Sony Alpha-A7 IV (PC Control)")
        self.double = FakeConnectedCamera()

    def app_calls_on_the_camera(self):
        found = re.findall(r"\b(?:cam|app\.state\.camera|old)\.(\w+)", APP_TEXT)
        return set(found) | {"close"}  # reached via camera.disconnect()

    def test_the_scan_found_the_camera_call_sites(self):
        self.assertIn("capture", self.app_calls_on_the_camera())
        self.assertIn("list_settings", self.app_calls_on_the_camera())

    def test_the_real_camera_answers_every_call_app_py_makes(self):
        missing = {name for name in self.app_calls_on_the_camera()
                   if not hasattr(self.real, name)}
        self.assertEqual(missing, set(), f"Gphoto2Camera is missing {missing}")

    def test_the_double_answers_every_call_app_py_makes(self):
        missing = {name for name in self.app_calls_on_the_camera()
                   if not hasattr(self.double, name)}
        self.assertEqual(missing, set(), f"FakeConnectedCamera is missing {missing}")

    def test_the_double_exposes_no_more_than_the_real_camera(self):
        public = {name for name in dir(self.double)
                  if not name.startswith("_")} - self.DOUBLE_ONLY

        missing = {name for name in public if not hasattr(self.real, name)}
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
