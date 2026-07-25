import os
import re
import unittest

from tests import support
from tests.fakes import fake_gphoto2
from tests.fakes.fake_camera import (FakeCameraFile, FakeConnectedCamera, FakeDevice, FakeWidget)

from camera.gp2 import Gphoto2Camera


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


GP2_TEXT = read(os.path.join(support.REPO_ROOT, "camera", "gp2.py"))
APP_TEXT = read(os.path.join(support.REPO_ROOT, "app.py"))


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
