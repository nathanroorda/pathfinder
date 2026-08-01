import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from tests import support

import gphoto2 as gp
from camera import gp2
from tests.fakes.fake_camera import FakeDevice

A7IV = "Sony Alpha-A7 IV (PC Control)"
GENERIC = "Canon EOS R5"


def lock_is_held(cam):
    acquired = cam._lock.acquire(blocking=False)
    if acquired:
        cam._lock.release()
    return not acquired


class CameraTestCase(unittest.TestCase):
    model = A7IV

    def setUp(self):
        self.clock = support.FakeClock()
        self.device = FakeDevice(clock=self.clock)
        patcher = mock.patch.object(gp2, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cam = gp2.Gphoto2Camera(self.device, self.model)
        self.save_dir = tempfile.mkdtemp(prefix="pathfinder-test-")
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)

    def driven(self):
        return [(c[1], c[2]) for c in self.device.calls_named("set_single_config")]

    def writes_of(self, widget, value):
        return [d for d in self.driven() if d == (widget, value)]

    def methods(self):
        return [c[0] for c in self.device.calls]

    def fail_writes(self, widget, value, times, error=None):
        error = error or gp.GPhoto2Error(gp.GP_ERROR_IO_WRITE)
        remaining = [times]

        def hook(method, *args):
            if (method == "set_single_config" and args[0] == widget
                    and args[1] == value and remaining[0]):
                remaining[0] -= 1
                raise error
        self.device.hook = hook


class Capture(CameraTestCase):
    def test_downloads_the_file_and_returns_its_path(self):
        path = self.cam.capture(save_dir=self.save_dir)

        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), self.device.image_bytes)
        self.assertEqual(os.path.dirname(path), self.save_dir)
        self.assertTrue(os.path.basename(path).endswith("_DSC00001.ARW"))

    def test_downloads_the_file_the_body_actually_reported(self):
        self.device.capture_results = [gp.CameraFilePath("/DCIM/999MSDCF", "X_1234.JPG")]

        self.cam.capture(save_dir=self.save_dir)

        folder, name, ftype = self.device.calls_named("file_get")[0][1:]
        self.assertEqual((folder, name), ("/DCIM/999MSDCF", "X_1234.JPG"))
        self.assertEqual(ftype, gp.GP_FILE_TYPE_NORMAL)

    def test_creates_the_capture_directory_if_it_is_missing(self):
        path = self.cam.capture(save_dir=os.path.join(self.save_dir, "a", "b"))
        self.assertTrue(os.path.isfile(path))

    def test_stale_events_are_drained_before_the_shutter_fires(self):
        self.device.events = [(gp.GP_EVENT_FILE_ADDED, gp.CameraFilePath()),
                              (gp.GP_EVENT_CAPTURE_COMPLETE, None)]

        self.cam.capture(save_dir=self.save_dir)

        methods = self.methods()
        self.assertLess(methods.index("wait_for_event"), methods.index("capture"))
        self.assertEqual(self.device.events, [])

    def test_first_shot_does_not_wait_out_the_shot_gap(self):
        self.cam.capture(save_dir=self.save_dir)
        self.assertEqual(self.clock.sleeps, [])

    def test_back_to_back_shots_wait_out_the_shot_gap(self):
        self.cam.capture(save_dir=self.save_dir)
        self.clock.sleeps.clear()

        self.cam.capture(save_dir=self.save_dir)

        self.assertEqual(self.clock.sleeps, [self.cam._quirks["shot_gap"]])

    def test_no_wait_once_the_gap_has_already_elapsed(self):
        self.cam.capture(save_dir=self.save_dir)
        self.clock.advance(60.0)
        self.clock.sleeps.clear()

        self.cam.capture(save_dir=self.save_dir)

        self.assertEqual(self.clock.sleeps, [])

    def test_refused_while_recording(self):
        self.cam.recording = True

        with self.assertRaisesRegex(RuntimeError, "while recording"):
            self.cam.capture(save_dir=self.save_dir)

        self.assertEqual(self.device.calls_named("capture"), [])

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.capture(save_dir=self.save_dir)

    def test_holds_the_bus_mutex_for_the_whole_operation(self):
        seen = []
        self.device.hook = lambda method, *a: seen.append(lock_is_held(self.cam))

        self.cam.capture(save_dir=self.save_dir)

        self.assertTrue(seen and all(seen))


class CaptureRetry(CameraTestCase):
    def test_retries_a_generic_error_then_succeeds(self):
        self.device.capture_results = [gp.GPhoto2Error(gp.GP_ERROR),
                                       gp.CameraFilePath()]

        path = self.cam.capture(save_dir=self.save_dir)

        self.assertTrue(os.path.isfile(path))
        self.assertEqual(len(self.device.calls_named("capture")), 2)

    def test_settles_the_bus_before_retrying(self):
        self.device.capture_results = [gp.GPhoto2Error(gp.GP_ERROR),
                                       gp.CameraFilePath()]

        self.cam.capture(save_dir=self.save_dir)

        self.assertIn(1.0, self.clock.sleeps)

    def test_gives_up_after_the_configured_attempts(self):
        attempts = self.cam._quirks["capture_retry_attempts"]
        self.device.capture_results = [gp.GPhoto2Error(gp.GP_ERROR)] * (attempts + 2)

        with self.assertRaises(gp.GPhoto2Error):
            self.cam.capture(save_dir=self.save_dir)

        self.assertEqual(len(self.device.calls_named("capture")), attempts)

    def test_transport_errors_are_not_retried(self):
        self.device.capture_results = [gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND),
                                       gp.CameraFilePath()]

        with self.assertRaises(gp.GPhoto2Error) as caught:
            self.cam.capture(save_dir=self.save_dir)

        self.assertEqual(caught.exception.code, gp.GP_ERROR_IO_USB_FIND)
        self.assertEqual(len(self.device.calls_named("capture")), 1)
        self.assertTrue(gp2.is_disconnect_error(caught.exception))

    def test_generic_body_does_not_retry_at_all(self):
        cam = gp2.Gphoto2Camera(self.device, GENERIC)
        self.device.capture_results = [gp.GPhoto2Error(gp.GP_ERROR)] * 3

        with self.assertRaises(gp.GPhoto2Error):
            cam.capture(save_dir=self.save_dir)

        self.assertEqual(len(self.device.calls_named("capture")), 1)


class DrainEvents(CameraTestCase):
    def endless_events(self, event=None):
        event = event or (gp.GP_EVENT_UNKNOWN, None)

        def hook(method, *args):
            if method == "wait_for_event":
                self.device.events.append(event)
        self.device.hook = hook

    def test_drains_the_queue_then_stops_at_the_first_quiet_poll(self):
        self.device.events = [(gp.GP_EVENT_UNKNOWN, None),
                              (gp.GP_EVENT_CAPTURE_COMPLETE, None)]

        self.cam._drain_events()

        self.assertEqual(self.device.events, [])
        self.assertEqual(len(self.device.calls_named("wait_for_event")), 3)

    def test_a_body_that_never_goes_quiet_does_not_hang_the_drain(self):
        self.endless_events()
        start = self.clock.monotonic()

        self.cam._drain_events()

        self.assertLess(self.clock.monotonic() - start, gp2.DRAIN_TIMEOUT + 1.0)

    def test_the_bound_holds_on_the_real_clock(self):
        import time as real_time

        self.endless_events()

        with mock.patch.object(gp2, "time", real_time):
            start = real_time.monotonic()
            self.cam._drain_events(timeout=0.05)
            elapsed = real_time.monotonic() - start

        self.assertLess(elapsed, 1.0)

    def test_giving_up_on_a_noisy_body_is_logged(self):
        self.endless_events()

        with self.assertLogs("camera.gp2", level="WARNING") as captured:
            self.cam._drain_events()

        self.assertIn("events pending", "\n".join(captured.output))

    def test_the_bus_mutex_is_released_even_by_a_noisy_body(self):
        self.endless_events()

        self.cam.capture(save_dir=self.save_dir)

        self.assertFalse(lock_is_held(self.cam))

    def test_a_quiet_body_is_not_logged_about(self):
        with self.assertNoLogs("camera.gp2", level="WARNING"):
            self.cam._drain_events()

    def test_a_dead_bus_is_logged_rather_than_swallowed(self):
        self.device.events = [gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)]

        with self.assertLogs("camera.gp2", level="WARNING") as captured:
            self.cam._drain_events()

        self.assertIn("draining events failed", "\n".join(captured.output))

    def test_a_drain_failure_is_left_for_the_operation_that_follows(self):
        self.device.events = [gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)]
        self.device.capture_results = [gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)]

        with self.assertRaises(gp.GPhoto2Error) as caught:
            self.cam.capture(save_dir=self.save_dir)

        self.assertTrue(gp2.is_disconnect_error(caught.exception))

    def test_a_logical_error_while_draining_is_not_shouted_about(self):
        self.device.events = [gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED)]

        with self.assertNoLogs("camera.gp2", level="WARNING"):
            self.cam._drain_events()


class Bulb(CameraTestCase):
    def _deliver_image_on_release(self, after_failures=0):
        remaining = [after_failures]

        def hook(method, *args):
            if method == "set_single_config" and args[0] == "bulb" and args[1] == 0:
                if remaining[0]:
                    remaining[0] -= 1
                    raise gp.GPhoto2Error(gp.GP_ERROR_IO_WRITE)
                self.device.events.append((gp.GP_EVENT_FILE_ADDED, gp.CameraFilePath()))
        self.device.hook = hook

    def test_opens_the_shutter_sleeps_then_closes_it(self):
        self._deliver_image_on_release()

        path = self.cam.bulb(4.0, save_dir=self.save_dir)

        self.assertEqual(self.driven(), [("bulb", 1), ("bulb", 0)])
        self.assertIn(4.0, self.clock.sleeps)
        self.assertEqual(self.device.value_of("bulb"), 0)
        self.assertTrue(os.path.isfile(path))

    def test_shutter_is_closed_even_if_the_exposure_is_interrupted(self):
        self.clock.sleep = mock.Mock(side_effect=KeyboardInterrupt)

        with self.assertRaises(KeyboardInterrupt):
            self.cam.bulb(4.0, save_dir=self.save_dir)

        self.assertEqual(self.device.value_of("bulb"), 0)
        self.assertEqual(self.driven(), [("bulb", 1), ("bulb", 0)])

    def test_shutter_release_is_retried_after_a_transient_write_failure(self):
        self._deliver_image_on_release(after_failures=1)

        path = self.cam.bulb(2.0, save_dir=self.save_dir)

        self.assertEqual(self.device.value_of("bulb"), 0)
        self.assertEqual(len(self.writes_of("bulb", 0)), 2)
        self.assertTrue(os.path.isfile(path))

    def test_a_release_that_never_lands_raises_the_transport_error(self):
        self._deliver_image_on_release(after_failures=99)

        with self.assertRaises(gp.GPhoto2Error) as caught:
            self.cam.bulb(1.0, save_dir=self.save_dir)

        self.assertTrue(gp2.is_disconnect_error(caught.exception))
        self.assertEqual(len(self.writes_of("bulb", 0)), gp2.RELEASE_ATTEMPTS)

    def test_a_release_that_never_lands_is_logged_as_an_error(self):
        self._deliver_image_on_release(after_failures=99)

        with self.assertLogs("camera.gp2", level="ERROR") as captured:
            with self.assertRaises(gp.GPhoto2Error):
                self.cam.bulb(1.0, save_dir=self.save_dir)

        self.assertIn("latched", "\n".join(captured.output))

    def test_an_interrupted_exposure_keeps_its_own_error_when_the_release_fails(self):
        self._deliver_image_on_release(after_failures=99)
        real_sleep = self.clock.sleep

        def sleep(seconds):
            if seconds == 4.0:
                raise KeyboardInterrupt
            real_sleep(seconds)

        self.clock.sleep = sleep

        with self.assertRaises(KeyboardInterrupt):
            self.cam.bulb(4.0, save_dir=self.save_dir)

    def test_readout_timeout_reports_failure_with_the_shutter_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no image"):
            self.cam.bulb(2.0, save_dir=self.save_dir)

        self.assertEqual(self.device.value_of("bulb"), 0)

    def test_readout_waits_out_the_exposure_plus_a_margin(self):
        start = self.clock.monotonic()

        with self.assertRaises(RuntimeError):
            self.cam.bulb(60.0, save_dir=self.save_dir)

        readout = self.clock.monotonic() - start - 60.0
        self.assertGreater(readout, 60.0)
        self.assertLess(readout, 60.0 + gp2.BULB_READOUT_MARGIN + 1.0)

    def test_readout_polling_is_bounded(self):
        start = self.clock.monotonic()

        with self.assertRaises(RuntimeError):
            self.cam.bulb(1.0, save_dir=self.save_dir)

        self.assertLess(self.clock.monotonic() - start,
                        1.0 + 1.0 + gp2.BULB_READOUT_MARGIN + 1.0)

    def test_unsupported_on_a_body_with_no_bulb_widget(self):
        cam = gp2.Gphoto2Camera(self.device, GENERIC)

        with self.assertRaisesRegex(RuntimeError, "not supported"):
            cam.bulb(1.0, save_dir=self.save_dir)

        self.assertEqual(self.driven(), [])

    def test_refused_while_recording(self):
        self.cam.recording = True

        with self.assertRaisesRegex(RuntimeError, "while recording"):
            self.cam.bulb(1.0, save_dir=self.save_dir)

        self.assertEqual(self.driven(), [])

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.bulb(1.0, save_dir=self.save_dir)

    def test_holds_the_bus_mutex_across_the_exposure(self):
        held_during_exposure = []
        self._deliver_image_on_release()
        real_sleep = self.clock.sleep

        def watching_sleep(seconds):
            held_during_exposure.append(lock_is_held(self.cam))
            real_sleep(seconds)

        self.clock.sleep = watching_sleep
        self.cam.bulb(3.0, save_dir=self.save_dir)

        self.assertTrue(held_during_exposure and all(held_during_exposure))

    def test_respects_the_shot_gap_after_a_still(self):
        self._deliver_image_on_release()
        self.cam.capture(save_dir=self.save_dir)
        self.clock.sleeps.clear()

        self.cam.bulb(2.0, save_dir=self.save_dir)

        self.assertEqual(self.clock.sleeps, [self.cam._quirks["shot_gap"], 2.0])


class Recording(CameraTestCase):
    def test_start_drives_the_movie_widget_and_updates_state(self):
        self.assertIs(self.cam.set_recording(True), True)

        self.assertEqual(self.device.value_of("movie"), 1)
        self.assertTrue(self.cam.recording)

    def test_stop_drives_the_movie_widget_back(self):
        self.cam.set_recording(True)

        self.assertIs(self.cam.set_recording(False), False)

        self.assertEqual(self.device.value_of("movie"), 0)
        self.assertFalse(self.cam.recording)

    def test_setting_the_current_state_touches_no_hardware(self):
        self.assertIs(self.cam.set_recording(False), False)
        self.assertEqual(self.device.calls, [])

    def test_repeat_start_is_idempotent(self):
        self.cam.set_recording(True)
        before = len(self.device.calls_named("set_config"))

        self.cam.set_recording(True)

        self.assertEqual(len(self.device.calls_named("set_config")), before)

    def test_truthy_values_are_normalised_to_bool(self):
        self.assertIs(self.cam.set_recording("start"), True)
        self.assertIs(self.cam.recording, True)

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.set_recording(True)

    def test_preview_is_refused_while_recording(self):
        self.cam.set_recording(True)
        with self.assertRaisesRegex(RuntimeError, "while recording"):
            self.cam.preview()


class Autofocus(CameraTestCase):
    def test_switches_out_of_manual_before_driving_af(self):
        self.device.config.get_child_by_name("focusmode").value = "Manual"

        mode = self.cam.autofocus()

        self.assertEqual(mode, "AF-A")
        self.assertEqual(self.device.value_of("focusmode"), "AF-A")

    def test_leaves_an_already_acceptable_mode_alone(self):
        self.device.config.get_child_by_name("focusmode").value = "AF-C"

        mode = self.cam.autofocus()

        self.assertEqual(mode, "AF-C")
        self.assertEqual(self.device.value_of("focusmode"), "AF-C")
        self.assertEqual(self.device.calls_named("set_config"), [])

    def test_drives_a_press_release_pair_in_order(self):
        self.cam.autofocus()

        self.assertEqual(self.driven(), [("autofocus", 1), ("autofocus", 0)])

    def test_generic_body_uses_the_ptp_widget_and_a_single_edge(self):
        cam = gp2.Gphoto2Camera(self.device, GENERIC)

        mode = cam.autofocus()

        self.assertIsNone(mode)
        self.assertEqual(self.driven(), [("autofocusdrive", 1)])
        self.assertEqual(self.device.calls_named("set_config"), [])

    def test_release_is_retried_after_a_transient_write_failure(self):
        self.fail_writes("autofocus", 0, times=1)

        self.cam.autofocus()

        self.assertEqual(self.device.value_of("autofocus"), 0)
        self.assertEqual(len(self.writes_of("autofocus", 0)), 2)

    def test_a_release_that_never_lands_raises_the_transport_error(self):
        self.fail_writes("autofocus", 0, times=99)

        with self.assertRaises(gp.GPhoto2Error) as caught:
            self.cam.autofocus()

        self.assertTrue(gp2.is_disconnect_error(caught.exception))
        self.assertEqual(len(self.writes_of("autofocus", 0)), gp2.RELEASE_ATTEMPTS)

    def test_a_failed_press_still_attempts_the_release(self):
        self.fail_writes("autofocus", 1, times=99)

        with self.assertRaises(gp.GPhoto2Error):
            self.cam.autofocus()

        self.assertEqual(len(self.writes_of("autofocus", 0)), 1)

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.autofocus()


class ManualFocus(CameraTestCase):
    def test_switches_into_manual_before_driving_the_motor(self):
        self.device.config.get_child_by_name("focusmode").value = "AF-C"

        mode = self.cam.manual_focus(3)

        self.assertEqual(mode, "Manual")
        self.assertEqual(self.device.value_of("focusmode"), "Manual")

    def test_step_is_coerced_to_the_widget_range_type(self):
        self.device.config.get_child_by_name("focusmode").value = "Manual"

        self.cam.manual_focus(-3)

        self.assertEqual(self.driven(), [("manualfocus", -3.0)])
        self.assertIsInstance(self.driven()[0][1], float)

    def test_sign_selects_direction(self):
        self.cam.manual_focus(5)
        self.cam.manual_focus(-5)

        self.assertEqual([v for _, v in self.driven()], [5.0, -5.0])

    def test_already_manual_costs_no_config_write(self):
        self.device.config.get_child_by_name("focusmode").value = "Manual"

        self.cam.manual_focus(1)

        self.assertEqual(self.device.calls_named("set_config"), [])

    def test_generic_body_uses_the_ptp_widget(self):
        cam = gp2.Gphoto2Camera(self.device, GENERIC)
        cam.manual_focus(2)
        self.assertEqual(self.driven(), [("manualfocusdrive", 2.0)])

    def test_step_is_clamped_to_the_widget_range(self):
        low, high, _ = self.device.config.get_child_by_name("manualfocus").get_range()

        self.cam.manual_focus(100000)
        self.cam.manual_focus(-100000)

        self.assertEqual([v for _, v in self.driven()], [high, low])

    def test_a_clamped_step_still_reports_the_focus_mode(self):
        self.assertEqual(self.cam.manual_focus(100000), "Manual")


class AfPoint(CameraTestCase):
    def test_centre_tap_maps_to_the_middle_of_the_af_grid(self):
        self.cam.set_af_point(0.5, 0.5)
        self.assertEqual(self.driven(), [("spotfocusarea", "320,240")])

    def test_corners_map_to_the_grid_bounds(self):
        self.cam.set_af_point(0.0, 0.0)
        self.cam.set_af_point(1.0, 1.0)
        self.assertEqual([v for _, v in self.driven()], ["0,0", "640,480"])

    def test_out_of_range_taps_are_clamped_not_rejected(self):
        self.cam.set_af_point(-3.0, 12.0)
        self.assertEqual(self.driven(), [("spotfocusarea", "0,480")])

    def test_unsupported_on_a_body_with_no_af_area_widget(self):
        cam = gp2.Gphoto2Camera(self.device, GENERIC)

        with self.assertRaisesRegex(RuntimeError, "not supported"):
            cam.set_af_point(0.5, 0.5)

        self.assertEqual(self.driven(), [])

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.set_af_point(0.5, 0.5)


class Magnifier(CameraTestCase):
    def lagging_read_back(self, reads):
        written = []
        remaining = [reads]

        def hook(method, *args):
            if method == "set_single_config" and args[0] == "focusmagnifier":
                written.append(args[1])
            elif method == "get_config" and written:
                widget = self.device.config.get_child_by_name("focusmagnifier")
                if remaining[0]:
                    remaining[0] -= 1
                    widget.value = "Off,332,249"
                else:
                    widget.value = written[-1]
        self.device.hook = hook

    def test_reports_the_levels_the_body_offers(self):
        self.assertEqual(self.cam.magnifier(), {
            "supported": True,
            "levels": ["Off", "1", "5.5", "11"],
            "value": "Off",
        })

    def test_the_position_the_body_appends_is_stripped_from_the_level(self):
        self.device.config.get_child_by_name("focusmagnifier").value = "5.5,332,249"
        self.assertEqual(self.cam.magnifier()["value"], "5.5")

    def test_setting_a_level_drives_the_widget_and_reports_the_new_state(self):
        state = self.cam.set_magnifier("5.5")

        self.assertEqual(self.driven(), [("focusmagnifier", "5.5")])
        self.assertEqual(state["value"], "5.5")

    def test_the_read_back_goes_through_the_whole_tree_not_single_config(self):
        # get_single_config serves a stale Sony property cache after a write.
        self.cam.set_magnifier("5.5")

        reads = self.methods()[self.methods().index("set_single_config"):]

        self.assertIn("get_config", reads)
        self.assertNotIn("get_single_config", reads)

    def test_a_settled_change_costs_exactly_one_tree_read(self):
        # A tree read is ~406ms on the a7 IV and holds the bus; validating the
        # level off one used to double the cost of every change (TODO #49).
        self.cam.set_magnifier("5.5")
        self.assertEqual(len(self.device.calls_named("get_config")), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_lagging_read_back_is_retried(self):
        self.lagging_read_back(reads=1)

        state = self.cam.set_magnifier("5.5")

        self.assertEqual(state["value"], "5.5")
        self.assertEqual(len(self.device.calls_named("get_config")), 2)

    def test_a_body_that_never_reflects_the_write_reports_the_truth(self):
        self.lagging_read_back(reads=10_000)

        with self.assertLogs("camera.gp2", level="WARNING") as captured:
            state = self.cam.set_magnifier("5.5")

        self.assertEqual(state["value"], "Off")
        self.assertIn("magnifier still reads", captured.output[0])
        self.assertEqual(len(self.device.calls_named("get_config")),
                         gp2.MAGNIFIER_SETTLE_ATTEMPTS)

    def test_the_retries_are_bounded_and_hold_the_bus(self):
        self.lagging_read_back(reads=10_000)
        seen = []
        outer = self.device.hook
        self.device.hook = lambda method, *a: (
            outer(method, *a), seen.append(lock_is_held(self.cam)))

        self.cam.set_magnifier("5.5")

        self.assertTrue(seen and all(seen))
        self.assertEqual(self.clock.sleeps,
                         [gp2.MAGNIFIER_SETTLE_DELAY] *
                         (gp2.MAGNIFIER_SETTLE_ATTEMPTS - 1))

    def test_a_numeric_level_is_coerced_to_the_choice_string(self):
        self.cam.set_magnifier(11)
        self.assertEqual(self.driven(), [("focusmagnifier", "11")])

    def test_a_level_the_body_does_not_offer_is_rejected_before_the_bus(self):
        with self.assertRaisesRegex(ValueError, "not a magnification"):
            self.cam.set_magnifier("2.5")

        self.assertEqual(self.driven(), [])

    def test_switching_off_is_retried_like_a_release(self):
        self.fail_writes("focusmagnifier", "Off", times=2)

        self.cam.set_magnifier("Off")

        self.assertEqual(len(self.writes_of("focusmagnifier", "Off")), 3)

    def test_switching_on_is_not_retried(self):
        self.fail_writes("focusmagnifier", "11", times=1)

        with self.assertRaises(gp.GPhoto2Error):
            self.cam.set_magnifier("11")

        self.assertEqual(len(self.writes_of("focusmagnifier", "11")), 1)

    def test_unsupported_on_a_body_with_no_magnifier_widget(self):
        cam = gp2.Gphoto2Camera(self.device, GENERIC)

        self.assertEqual(cam.magnifier(),
                         {"supported": False, "levels": [], "value": None})
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            cam.set_magnifier("5.5")

        self.assertEqual(self.driven(), [])

    def test_refused_after_close(self):
        self.cam.close()
        for call in (self.cam.magnifier, lambda: self.cam.set_magnifier("Off")):
            with self.assertRaises(gp2.CameraDisconnected):
                call()


class ListSettings(CameraTestCase):
    def names(self):
        return [s["name"] for s in self.cam.list_settings()]

    def test_returns_the_user_facing_sections_only(self):
        self.assertEqual(self.names(),
                         ["iso", "whitebalance", "f-number", "burstnumber",
                          "focusmode", "datetime"])

    def test_action_widgets_are_never_exposed_as_settings(self):
        for hidden in ("bulb", "movie", "autofocus", "manualfocus",
                       "spotfocusarea", "focusmagnifier"):
            self.assertNotIn(hidden, self.names())

    def test_status_widgets_are_not_settings(self):
        self.assertNotIn("batterylevel", self.names())

    def test_readonly_widgets_are_omitted(self):
        self.assertNotIn("imagequality", self.names())

    def test_unrenderable_widget_types_are_omitted(self):
        self.assertNotIn("liveviewsize", self.names())

    def test_nested_sections_are_flattened(self):
        self.assertIn("datetime", self.names())

    def test_rows_carry_what_the_browser_needs_to_render_them(self):
        by_name = {s["name"]: s for s in self.cam.list_settings()}

        self.assertEqual(by_name["iso"]["type"], "choice")
        self.assertEqual(by_name["iso"]["choices"], ["100", "400", "800"])
        self.assertEqual(by_name["burstnumber"]["type"], "range")
        self.assertEqual(by_name["burstnumber"]["min"], 1.0)
        self.assertEqual(by_name["datetime"]["type"], "text")

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.list_settings()


class SetSetting(CameraTestCase):
    def test_writes_the_value_and_pushes_the_config(self):
        self.cam.set_setting("iso", "800")

        self.assertEqual(self.device.value_of("iso"), "800")
        self.assertEqual(len(self.device.calls_named("set_config")), 1)

    def test_values_are_coerced_to_the_widget_type(self):
        self.cam.set_setting("iso", 800)
        self.cam.set_setting("burstnumber", "5")

        self.assertEqual(self.device.value_of("iso"), "800")
        self.assertEqual(self.device.value_of("burstnumber"), 5.0)

    def test_range_sliders_are_clamped_too(self):
        _, high, _ = self.device.config.get_child_by_name("burstnumber").get_range()

        self.cam.set_setting("burstnumber", 9999)

        self.assertEqual(self.device.value_of("burstnumber"), high)

    def test_unknown_widget_raises_instead_of_writing_something_else(self):
        with self.assertRaises(KeyError):
            self.cam.set_setting("no-such-widget", "1")

        self.assertEqual(self.device.calls_named("set_config"), [])

    def test_readonly_widget_is_not_settable(self):
        with self.assertRaises(KeyError):
            self.cam.set_setting("imagequality", "JPEG")

    def test_every_listed_setting_is_writable(self):
        for setting in self.cam.list_settings():
            with self.subTest(name=setting["name"]):
                self.cam.set_setting(setting["name"], setting["value"])

    def test_nothing_outside_the_listing_is_writable(self):
        listed = {s["name"] for s in self.cam.list_settings()}

        for name in ("bulb", "movie", "autofocus", "autofocusdrive",
                     "manualfocus", "manualfocusdrive", "spotfocusarea",
                     "batterylevel", "lensname", "imagequality",
                     "liveviewsize", "no-such-widget"):
            with self.subTest(name=name):
                self.assertNotIn(name, listed)
                with self.assertRaises(KeyError):
                    self.cam.set_setting(name, 1)

    def test_a_refused_write_reaches_no_widget(self):
        with self.assertRaises(KeyError):
            self.cam.set_setting("bulb", 1)

        self.assertEqual(self.device.value_of("bulb"), 0)
        self.assertEqual(self.device.calls_named("set_config"), [])
        self.assertEqual(self.driven(), [])

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.set_setting("iso", "800")


class Telemetry(CameraTestCase):
    def test_returns_the_status_section(self):
        by_name = {t["name"]: t for t in self.cam.telemetry()}

        self.assertEqual(by_name["batterylevel"]["value"], "87%")
        self.assertEqual(by_name["lensname"]["label"], "Lens Name")

    def test_excludes_settings_sections(self):
        self.assertNotIn("iso", [t["name"] for t in self.cam.telemetry()])

    def test_one_unreadable_widget_does_not_fail_the_panel(self):
        by_name = {t["name"]: t for t in self.cam.telemetry()}

        self.assertIsNone(by_name["serialnumber"]["value"])
        self.assertEqual(len(by_name), 3)

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.telemetry()


class Preview(CameraTestCase):
    def test_returns_jpeg_bytes(self):
        frame = self.cam.preview()

        self.assertIsInstance(frame, bytes)
        self.assertTrue(frame.startswith(b"\xff\xd8"))

    def test_each_call_pulls_a_fresh_frame(self):
        self.device.preview_frames = [b"\xff\xd8one", b"\xff\xd8two"]

        self.assertEqual(self.cam.preview(), b"\xff\xd8one")
        self.assertEqual(self.cam.preview(), b"\xff\xd8two")

    def test_refused_after_close(self):
        self.cam.close()
        with self.assertRaises(gp2.CameraDisconnected):
            self.cam.preview()


class CloseAndReuse(CameraTestCase):
    def test_close_releases_the_usb_claim(self):
        self.cam.close()
        self.assertEqual(self.device.exit_count, 1)

    def test_close_is_idempotent(self):
        self.cam.close()
        self.cam.close()
        self.assertEqual(self.device.exit_count, 1)

    def test_every_operation_refuses_a_closed_connection(self):
        self.cam.close()
        operations = {
            "capture": lambda: self.cam.capture(save_dir=self.save_dir),
            "bulb": lambda: self.cam.bulb(1.0, save_dir=self.save_dir),
            "preview": self.cam.preview,
            "set_recording": lambda: self.cam.set_recording(True),
            "autofocus": self.cam.autofocus,
            "manual_focus": lambda: self.cam.manual_focus(1),
            "set_af_point": lambda: self.cam.set_af_point(0.5, 0.5),
            "magnifier": self.cam.magnifier,
            "set_magnifier": lambda: self.cam.set_magnifier("Off"),
            "list_settings": self.cam.list_settings,
            "set_setting": lambda: self.cam.set_setting("iso", "800"),
            "telemetry": self.cam.telemetry,
        }
        for name, call in operations.items():
            with self.subTest(operation=name):
                with self.assertRaises(gp2.CameraDisconnected):
                    call()

    def test_a_closed_connection_reads_as_a_disconnect(self):
        self.assertTrue(gp2.is_disconnect_error(gp2.CameraDisconnected("closed")))

    def test_disconnect_closes_the_camera(self):
        import camera

        camera.disconnect(self.cam)

        self.assertEqual(self.device.exit_count, 1)


class Serialisation(CameraTestCase):
    def test_concurrent_captures_do_not_overlap(self):
        import time as real_time

        depth = 0
        overlaps = []
        guard = threading.Lock()

        def hook(method, *args):
            nonlocal depth
            if method != "capture":
                return
            with guard:
                depth += 1
                overlaps.append(depth)
            real_time.sleep(0.02)
            with guard:
                depth -= 1

        self.device.hook = hook
        self.cam._quirks = dict(self.cam._quirks, shot_gap=0.0)
        with mock.patch.object(gp2, "time", real_time):
            threads = [threading.Thread(
                target=lambda: self.cam.capture(save_dir=self.save_dir))
                for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(max(overlaps), 1)
        self.assertEqual(len(self.device.calls_named("capture")), 4)


class BusTimeout(CameraTestCase):
    def setUp(self):
        super().setUp()
        for name in ("BUS_TIMEOUT", "PREVIEW_BUS_TIMEOUT"):
            patcher = mock.patch.object(gp2, name, 0.05)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.released = threading.Event()

    def hold_the_bus(self):
        entered = threading.Event()

        def hook(method, *args):
            if method == "capture":
                entered.set()
                self.released.wait(10)
        self.device.hook = hook
        holder = threading.Thread(
            target=lambda: self.cam.capture(save_dir=self.save_dir), daemon=True)

        def let_go():
            self.released.set()
            holder.join(10)
        self.addCleanup(let_go)

        holder.start()
        self.assertTrue(entered.wait(10), "the holding thread never took the bus")

    def test_a_second_operation_is_refused_rather_than_queued_behind_the_first(self):
        self.hold_the_bus()

        with self.assertRaises(gp2.CameraBusy):
            self.cam.telemetry()

    def test_the_refusal_names_the_operation_holding_the_bus(self):
        self.hold_the_bus()

        with self.assertRaises(gp2.CameraBusy) as caught:
            self.cam.telemetry()

        self.assertIn("capture", str(caught.exception))

    def test_the_wait_is_bounded_even_though_the_holder_is_not(self):
        import time as real_time

        self.hold_the_bus()

        start = real_time.monotonic()
        with self.assertRaises(gp2.CameraBusy):
            self.cam.telemetry()

        self.assertLess(real_time.monotonic() - start, 2.0)

    def test_a_bus_that_frees_up_in_time_is_waited_for_not_refused(self):
        with mock.patch.object(gp2, "BUS_TIMEOUT", 5.0):
            self.hold_the_bus()
            threading.Timer(0.05, self.released.set).start()

            self.cam.telemetry()   # waits out the holder rather than refusing

    def test_a_refused_operation_sends_nothing_to_the_body(self):
        self.hold_the_bus()
        before = len(self.device.calls)

        with self.assertRaises(gp2.CameraBusy):
            self.cam.set_setting("iso", "800")

        self.assertEqual(len(self.device.calls), before)

    def test_every_operation_refuses_a_held_bus(self):
        self.hold_the_bus()
        operations = {
            "bulb": lambda: self.cam.bulb(1.0, save_dir=self.save_dir),
            "preview": self.cam.preview,
            "set_recording": lambda: self.cam.set_recording(True),
            "autofocus": self.cam.autofocus,
            "manual_focus": lambda: self.cam.manual_focus(1),
            "set_af_point": lambda: self.cam.set_af_point(0.5, 0.5),
            "magnifier": self.cam.magnifier,
            "set_magnifier": lambda: self.cam.set_magnifier("Off"),
            "list_settings": self.cam.list_settings,
            "set_setting": lambda: self.cam.set_setting("iso", "800"),
            "telemetry": self.cam.telemetry,
            "close": self.cam.close,
        }
        for name, call in operations.items():
            with self.subTest(operation=name):
                with self.assertRaises(gp2.CameraBusy):
                    call()

    def test_a_second_capture_is_refused_too(self):
        self.hold_the_bus()

        with self.assertRaises(gp2.CameraBusy):
            self.cam.capture(save_dir=self.save_dir)

    def test_preview_gives_up_sooner_than_the_operations_worth_waiting_for(self):
        import time as real_time

        self.assertLess(gp2.PREVIEW_BUS_TIMEOUT, 1.0)
        with mock.patch.object(gp2, "BUS_TIMEOUT", 30.0):
            self.hold_the_bus()

            start = real_time.monotonic()
            with self.assertRaises(gp2.CameraBusy):
                self.cam.preview()

            self.assertLess(real_time.monotonic() - start, 2.0)

    def test_being_busy_is_not_mistaken_for_a_disconnect(self):
        self.assertFalse(gp2.is_disconnect_error(gp2.CameraBusy("busy")))

    def test_a_close_that_cannot_take_the_bus_leaves_the_handle_alone(self):
        self.hold_the_bus()

        with self.assertLogs("camera.gp2", level="ERROR") as captured:
            with self.assertRaises(gp2.CameraBusy):
                self.cam.close()

        self.assertIn("USB claim", "\n".join(captured.output))
        self.assertEqual(self.device.exit_count, 0)
        self.assertIsNotNone(self.cam._cam)

    def test_the_bus_is_usable_again_once_the_holder_finishes(self):
        self.hold_the_bus()
        with self.assertRaises(gp2.CameraBusy):
            self.cam.telemetry()

        self.released.set()

        for _ in range(200): # the holder still has to finish its download
            try:
                self.cam.telemetry()
                break
            except gp2.CameraBusy:
                continue
        else:
            self.fail("the bus never came back after the holder let go")

    def test_a_free_bus_is_never_refused(self):
        self.cam.telemetry()
        self.cam.telemetry()
        self.assertFalse(lock_is_held(self.cam))


class Connect(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice()
        patcher = mock.patch.object(gp2.gp, "Camera", return_value=self.device)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_initialises_the_device_and_reads_its_model(self):
        cam = gp2.connect()

        self.assertEqual(cam.model, "Sony Alpha-A7 IV (PC Control)")
        self.assertEqual(self.device.calls_named("init"), [("init",)])

    def test_resolves_quirks_from_the_reported_model(self):
        self.assertEqual(gp2.connect()._quirks["af_widget"], "autofocus")

    def test_an_unreadable_model_falls_back_to_a_generic_name(self):
        self.device.abilities_error = gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED)

        cam = gp2.connect()

        self.assertEqual(cam.model, "USB camera (gphoto2)")
        self.assertEqual(cam._quirks, gp2.DEFAULT_QUIRKS)

    def test_an_empty_model_falls_back_to_a_generic_name(self):
        self.device.model = ""
        self.assertEqual(gp2.connect().model, "USB camera (gphoto2)")

    def test_a_failed_handshake_propagates(self):
        self.device.init_error = gp.GPhoto2Error(gp.GP_ERROR_MODEL_NOT_FOUND)

        with self.assertRaises(gp.GPhoto2Error):
            gp2.connect()

    def test_a_new_connection_is_not_recording(self):
        self.assertFalse(gp2.connect().recording)


if __name__ == "__main__":
    unittest.main()
