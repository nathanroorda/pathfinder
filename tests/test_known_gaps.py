import shutil
import tempfile
import types
import unittest
from unittest import mock

from tests import support

import gphoto2 as gp
from camera import gp2
from tests.fakes.fake_camera import FakeDevice

A7IV = "Sony Alpha-A7 IV (PC Control)"


class KnownGapTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = support.FakeClock()
        self.device = FakeDevice(clock=self.clock)
        patcher = mock.patch.object(gp2, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cam = gp2.Gphoto2Camera(self.device, A7IV)
        self.save_dir = tempfile.mkdtemp(prefix="pathfinder-gap-")
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)


class QuirkLayering(unittest.TestCase):
    @unittest.expectedFailure  # TODO.md #13
    def test_a_vendor_table_need_not_repeat_every_default(self):
        partial_vendor = types.SimpleNamespace(
            quirks=lambda model: {"shot_gap": 0.5} if "acme" in model.lower() else None)

        with mock.patch.object(gp2, "VENDORS", [partial_vendor]):
            quirks = gp2._quirks_for("ACME Snapmaster")

        self.assertEqual(set(quirks), set(gp2.DEFAULT_QUIRKS))


class RetryBounds(KnownGapTestCase):
    @unittest.expectedFailure  # TODO.md #34
    def test_zero_retry_attempts_fails_clearly(self):
        self.cam._quirks = dict(self.cam._quirks, capture_retry_attempts=0)

        with self.assertRaises((RuntimeError, ValueError, gp.GPhoto2Error)):
            self.cam.capture(save_dir=self.save_dir)


if __name__ == "__main__":
    unittest.main()
