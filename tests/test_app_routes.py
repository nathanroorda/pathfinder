import asyncio
import unittest
from unittest import mock

from tests import support

import gphoto2 as gp
from camera import gp2
from tests.fakes.fake_camera import FakeConnectedCamera

if support.have("pydantic") and support.have("fastapi"):
    import app as app_module
    from fastapi import HTTPException


def run(coro):
    return asyncio.run(coro)


class StubRequest:
    def __init__(self, alive_for=2):
        self.alive_for = alive_for
        self.checks = 0

    async def is_disconnected(self):
        self.checks += 1
        return self.checks > self.alive_for


async def drain(response, limit=10):
    chunks = []
    try:
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if len(chunks) >= limit:
                break
    finally:
        await response.body_iterator.aclose()
    return chunks


@support.requires("fastapi", "pydantic")
class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.cam = FakeConnectedCamera()
        self.state = app_module.app.state
        self.state.camera = self.cam
        self.state.camera_warned = False
        # asyncio.Lock binds to the loop that first acquires it, and asyncio.run builds a fresh loop per test.
        app_module._connect_lock = asyncio.Lock()
        self.addCleanup(setattr, self.state, "camera", None)

    def assertHTTPStatus(self, status, call, *args):
        with self.assertRaises(HTTPException) as caught:
            run(call(*args))
        self.assertEqual(caught.exception.status_code, status)
        return caught.exception


class Status(RouteTestCase):
    def test_reports_a_connected_camera(self):
        self.assertEqual(run(app_module.status()), {
            "connected": True,
            "model": "Sony Alpha-A7 IV (PC Control)",
            "recording": False,
        })

    def test_reports_no_camera_without_raising(self):
        self.state.camera = None

        self.assertEqual(run(app_module.status()), {
            "connected": False, "model": None, "recording": False})

    def test_mirrors_the_recording_flag(self):
        self.cam.recording = True
        self.assertTrue(run(app_module.status())["recording"])


@support.requires("fastapi", "pydantic")
class NoCameraConnected(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.state.camera = None

    def test_routes_that_take_no_body(self):
        for name in ("capture", "record_start", "record_stop", "autofocus",
                     "telemetry", "get_settings"):
            with self.subTest(route=name):
                exc = self.assertHTTPStatus(503, getattr(app_module, name))
                self.assertIn("no camera", exc.detail)

    def test_routes_that_take_a_body(self):
        for route, body in [
                (app_module.bulb, app_module.BulbExposure(seconds=1)),
                (app_module.manual_focus, app_module.FocusStep(steps=1)),
                (app_module.af_point, app_module.AfPoint(x=0.5, y=0.5))]:
            with self.subTest(route=route.__name__):
                self.assertHTTPStatus(503, route, body)

    def test_set_setting(self):
        self.assertHTTPStatus(503, lambda: app_module.set_setting(
            "iso", app_module.SettingValue(value="800")))

    def test_liveview(self):
        self.assertHTTPStatus(503, lambda: app_module.liveview(StubRequest()))


class Capture(RouteTestCase):
    def test_returns_the_saved_path(self):
        self.assertEqual(run(app_module.capture()),
                         {"ok": True, "path": "captures/1774000000_DSC00001.ARW"})

    def test_busy_camera_is_a_conflict_not_a_failure(self):
        self.cam.errors["capture"] = RuntimeError(
            "cannot capture a still while recording")

        exc = self.assertHTTPStatus(409, app_module.capture)

        self.assertIn("recording", exc.detail)
        self.assertIs(self.state.camera, self.cam)


class DisconnectHandling(RouteTestCase):
    def test_transport_error_returns_503_and_drops_the_camera(self):
        self.cam.errors["capture"] = gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)

        exc = self.assertHTTPStatus(503, app_module.capture)

        self.assertIn("disconnected", exc.detail)
        self.assertIsNone(self.state.camera)

    def test_dropping_releases_the_usb_claim(self):
        self.cam.errors["capture"] = gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)

        self.assertHTTPStatus(503, app_module.capture)

        self.assertTrue(self.cam.closed)

    def test_dropping_rearms_the_connect_warning(self):
        self.state.camera_warned = True
        self.cam.errors["capture"] = gp2.CameraDisconnected("closed")

        self.assertHTTPStatus(503, app_module.capture)

        self.assertFalse(self.state.camera_warned)

    def test_every_hardware_route_drops_on_a_transport_error(self):
        cases = [
            ("capture", app_module.capture, ()),
            ("bulb", app_module.bulb, (app_module.BulbExposure(seconds=1),)),
            ("set_recording", app_module.record_start, ()),
            ("autofocus", app_module.autofocus, ()),
            ("manual_focus", app_module.manual_focus,
             (app_module.FocusStep(steps=1),)),
            ("set_af_point", app_module.af_point,
             (app_module.AfPoint(x=0.5, y=0.5),)),
            ("telemetry", app_module.telemetry, ()),
            ("list_settings", app_module.get_settings, ()),
        ]
        for method, route, args in cases:
            with self.subTest(route=route.__name__):
                self.setUp()
                self.cam.errors[method] = gp.GPhoto2Error(gp.GP_ERROR_IO)
                self.assertHTTPStatus(503, route, *args)
                self.assertIsNone(self.state.camera)

    def test_a_logical_gphoto_error_keeps_the_connection(self):
        self.cam.errors["set_setting"] = gp.GPhoto2Error(gp.GP_ERROR_BAD_PARAMETERS)

        self.assertHTTPStatus(400, lambda: app_module.set_setting(
            "iso", app_module.SettingValue(value="nonsense")))

        self.assertIs(self.state.camera, self.cam)

    def test_dropping_twice_is_harmless(self):
        run(app_module._drop_camera(RuntimeError("first")))
        run(app_module._drop_camera(RuntimeError("second")))

        self.assertIsNone(self.state.camera)
        self.assertEqual(len(self.cam.called("close")), 1)

    def test_a_close_that_fails_still_drops_the_camera(self):
        self.cam.close = mock.Mock(side_effect=gp.GPhoto2Error(gp.GP_ERROR_IO))

        run(app_module._drop_camera(RuntimeError("boom")))

        self.assertIsNone(self.state.camera)


class Bulb(RouteTestCase):
    def body(self, seconds=2.0):
        return app_module.BulbExposure(seconds=seconds)

    def test_passes_the_exposure_through_and_returns_the_path(self):
        result = run(app_module.bulb(self.body(2.0)))

        self.assertEqual(result["ok"], True)
        self.assertEqual(self.cam.called("bulb"), [("bulb", 2.0)])

    def test_unsupported_body_is_a_conflict(self):
        self.cam.errors["bulb"] = RuntimeError("bulb is not supported on this body")
        self.assertHTTPStatus(409, app_module.bulb, self.body())

    def test_a_bad_value_is_a_client_error(self):
        self.cam.errors["bulb"] = ValueError("sleep length must be non-negative")
        self.assertHTTPStatus(400, app_module.bulb, self.body())

    def test_a_disconnect_is_not_swallowed_as_a_400(self):
        self.cam.errors["bulb"] = gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)

        self.assertHTTPStatus(503, app_module.bulb, self.body())

        self.assertIsNone(self.state.camera)


class Recording(RouteTestCase):
    def test_start_and_stop_report_the_new_state(self):
        self.assertEqual(run(app_module.record_start()),
                         {"ok": True, "recording": True})
        self.assertEqual(run(app_module.record_stop()),
                         {"ok": True, "recording": False})

    def test_start_and_stop_drive_the_same_call_with_opposite_arguments(self):
        run(app_module.record_start())
        run(app_module.record_stop())

        self.assertEqual(self.cam.called("set_recording"),
                         [("set_recording", True), ("set_recording", False)])

    def test_a_refusal_is_a_client_error(self):
        self.cam.errors["set_recording"] = gp.GPhoto2Error(gp.GP_ERROR_NOT_SUPPORTED)
        self.assertHTTPStatus(400, app_module.record_start)


class Focus(RouteTestCase):
    def test_autofocus_reports_the_mode_it_settled_on(self):
        self.assertEqual(run(app_module.autofocus()),
                         {"ok": True, "focusmode": "AF-A"})

    def test_manual_focus_passes_the_step_through(self):
        result = run(app_module.manual_focus(app_module.FocusStep(steps=-3)))

        self.assertEqual(result, {"ok": True, "focusmode": "Manual"})
        self.assertEqual(self.cam.called("manual_focus"), [("manual_focus", -3)])

    def test_af_point_passes_both_axes_through(self):
        result = run(app_module.af_point(app_module.AfPoint(x=0.25, y=0.75)))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.cam.called("set_af_point"),
                         [("set_af_point", 0.25, 0.75)])

    def test_focus_failures_are_client_errors(self):
        for method, route, args in (
                ("autofocus", app_module.autofocus, ()),
                ("manual_focus", app_module.manual_focus,
                 (app_module.FocusStep(steps=1),)),
                ("set_af_point", app_module.af_point,
                 (app_module.AfPoint(x=0.5, y=0.5),))):
            with self.subTest(route=route.__name__):
                self.setUp()
                self.cam.errors[method] = gp.GPhoto2Error(gp.GP_ERROR_BAD_PARAMETERS)
                self.assertHTTPStatus(400, route, *args)


class Settings(RouteTestCase):
    def test_get_returns_the_camera_list_verbatim(self):
        self.assertEqual(run(app_module.get_settings()), self.cam.list_settings())

    def test_telemetry_returns_the_status_list_verbatim(self):
        self.assertEqual(run(app_module.telemetry()), self.cam.telemetry())

    def test_set_writes_then_returns_the_refreshed_list(self):
        result = run(app_module.set_setting(
            "iso", app_module.SettingValue(value="800")))

        names = [c[0] for c in self.cam.calls]
        self.assertLess(names.index("set_setting"), names.index("list_settings"))
        self.assertEqual(self.cam.called("set_setting"),
                         [("set_setting", "iso", "800")])
        self.assertEqual(result, self.cam.list_settings())

    def test_a_rejected_value_does_not_reach_the_refresh(self):
        self.cam.errors["set_setting"] = gp.GPhoto2Error(gp.GP_ERROR_BAD_PARAMETERS)

        self.assertHTTPStatus(400, lambda: app_module.set_setting(
            "iso", app_module.SettingValue(value="1600")))

        self.assertEqual(self.cam.called("list_settings"), [])

    def test_the_value_type_survives_the_round_trip(self):
        run(app_module.set_setting("capturemode", app_module.SettingValue(value=1)))

        self.assertIsInstance(self.cam.called("set_setting")[0][2], int)

    def test_a_widget_outside_the_listing_is_a_404_not_a_400(self):
        self.cam.errors["set_setting"] = KeyError("bulb")

        exc = self.assertHTTPStatus(404, lambda: app_module.set_setting(
            "bulb", app_module.SettingValue(value=1)))

        self.assertIn("bulb", exc.detail)
        self.assertEqual(self.cam.called("list_settings"), [])
        self.assertIs(self.state.camera, self.cam)


class Liveview(RouteTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(app_module, "LIVEVIEW_FRAME_INTERVAL", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _response(self, request):
        return await app_module.liveview(request)

    async def _collect(self, request, limit=10):
        return await drain(await app_module.liveview(request), limit)

    def test_frames_are_multipart_chunks_the_browser_can_render(self):
        chunks = run(self._collect(StubRequest(alive_for=1)))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0],
                         b"--pathfinderframe\r\n"
                         b"Content-Type: image/jpeg\r\n"
                         b"Content-Length: 6\r\n\r\n"
                         b"\xff\xd8jpeg\r\n")

    def test_boundary_matches_the_content_type_header(self):
        response = run(self._response(StubRequest(alive_for=0)))

        self.assertIn(f"boundary={app_module.LIVEVIEW_BOUNDARY}",
                      response.media_type)

    def test_stream_ends_when_the_client_goes_away(self):
        chunks = run(self._collect(StubRequest(alive_for=3), limit=100))

        self.assertEqual(len(chunks), 3)

    def test_stream_ends_when_the_camera_is_dropped(self):
        self.cam.errors["preview"] = gp.GPhoto2Error(gp.GP_ERROR_IO_USB_FIND)

        chunks = run(self._collect(StubRequest(alive_for=100), limit=100))

        self.assertEqual(chunks, [])
        self.assertIsNone(self.state.camera)

    def test_a_transient_frame_error_does_not_end_the_stream(self):
        self.cam.preview = mock.Mock(
            side_effect=[RuntimeError("camera busy"), b"\xff\xd8jpeg"])

        chunks = run(self._collect(StubRequest(alive_for=10), limit=1))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(self.cam.preview.call_count, 2)


class TryConnect(RouteTestCase):
    def test_success_installs_the_camera(self):
        self.state.camera = None

        with mock.patch.object(app_module.camera, "connect", return_value=self.cam):
            app_module._try_connect(app_module.app)

        self.assertIs(self.state.camera, self.cam)
        self.assertFalse(self.state.camera_warned)

    def test_failure_leaves_no_camera_installed(self):
        self.state.camera = None

        with mock.patch.object(app_module.camera, "connect",
                               side_effect=gp.GPhoto2Error(gp.GP_ERROR_MODEL_NOT_FOUND)):
            app_module._try_connect(app_module.app)

        self.assertIsNone(self.state.camera)

    def test_a_persistent_failure_is_warned_about_exactly_once(self):
        self.state.camera = None

        with mock.patch.object(app_module.camera, "connect",
                               side_effect=gp.GPhoto2Error(gp.GP_ERROR_MODEL_NOT_FOUND)):
            with self.assertLogs("app", level="DEBUG") as captured:
                for _ in range(5):
                    app_module._try_connect(app_module.app)

        warnings = [r for r in captured.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(self.state.camera_warned)

    def test_a_reconnect_rearms_the_warning(self):
        self.state.camera = None
        self.state.camera_warned = True

        with mock.patch.object(app_module.camera, "connect", return_value=self.cam):
            app_module._try_connect(app_module.app)

        self.assertFalse(self.state.camera_warned)


class ConnectRoute(RouteTestCase):
    def test_reports_an_already_connected_camera_without_reconnecting(self):
        with mock.patch.object(app_module.camera, "connect") as connect:
            result = run(app_module.connect())

        self.assertEqual(result, {"connected": True, "model": self.cam.model})
        connect.assert_not_called()

    def test_connects_on_demand(self):
        self.state.camera = None

        with mock.patch.object(app_module.camera, "connect", return_value=self.cam):
            result = run(app_module.connect())

        self.assertEqual(result["connected"], True)
        self.assertIs(self.state.camera, self.cam)

    def test_no_camera_found_is_503(self):
        self.state.camera = None

        with mock.patch.object(app_module.camera, "connect",
                               side_effect=gp.GPhoto2Error(gp.GP_ERROR_MODEL_NOT_FOUND)):
            exc = self.assertHTTPStatus(503, app_module.connect)

        self.assertIn("no camera", exc.detail)


class Watcher(RouteTestCase):
    def test_reconnects_while_no_camera_is_installed(self):
        self.state.camera = None
        reconnected = asyncio.Event()

        def fake_try_connect(app):
            app.state.camera = self.cam
            reconnected.set()

        async def scenario():
            with mock.patch.object(app_module, "_try_connect", fake_try_connect), \
                 mock.patch.object(app_module, "CAMERA_POLL_INTERVAL", 0.01):
                task = asyncio.create_task(app_module._camera_watcher(app_module.app))
                await asyncio.wait_for(reconnected.wait(), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        run(scenario())
        self.assertIs(self.state.camera, self.cam)

    def test_leaves_a_healthy_camera_alone(self):
        attempts = []

        async def scenario():
            with mock.patch.object(app_module, "_try_connect", attempts.append), \
                 mock.patch.object(app_module, "CAMERA_POLL_INTERVAL", 0):
                task = asyncio.create_task(app_module._camera_watcher(app_module.app))
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        run(scenario())
        self.assertEqual(attempts, [])


class Lifespan(RouteTestCase):
    def _install_camera(self, camera):
        return mock.patch.object(app_module, "_try_connect",
                                 lambda app: setattr(app.state, "camera", camera))

    def test_connects_at_startup(self):
        async def scenario():
            with self._install_camera(self.cam):
                async with app_module.lifespan(app_module.app):
                    return app_module.app.state.camera

        self.assertIs(run(scenario()), self.cam)

    def test_shutdown_stops_recording_before_disconnecting(self):
        self.cam.recording = True

        async def scenario():
            with self._install_camera(self.cam):
                async with app_module.lifespan(app_module.app):
                    pass

        run(scenario())

        names = [c[0] for c in self.cam.calls]
        self.assertEqual(self.cam.called("set_recording"),
                         [("set_recording", False)])
        self.assertLess(names.index("set_recording"), names.index("close"))

    def test_shutdown_releases_the_usb_claim(self):
        async def scenario():
            with self._install_camera(self.cam):
                async with app_module.lifespan(app_module.app):
                    pass

        run(scenario())
        self.assertTrue(self.cam.closed)

    def test_shutdown_survives_a_camera_that_will_not_answer(self):
        self.cam.recording = True
        self.cam.errors["set_recording"] = gp.GPhoto2Error(gp.GP_ERROR_IO)
        self.cam.close = mock.Mock(side_effect=gp.GPhoto2Error(gp.GP_ERROR_IO))

        async def scenario():
            with self._install_camera(self.cam):
                async with app_module.lifespan(app_module.app):
                    pass

        run(scenario())

    def test_shutdown_with_no_camera_is_a_no_op(self):
        async def scenario():
            with self._install_camera(None):
                async with app_module.lifespan(app_module.app):
                    pass

        run(scenario())

    def test_the_watcher_does_not_outlive_the_app(self):
        async def scenario():
            with self._install_camera(self.cam):
                async with app_module.lifespan(app_module.app):
                    others = [t for t in asyncio.all_tasks()
                              if t is not asyncio.current_task()]
            return others

        others = run(scenario())

        self.assertEqual(len(others), 1)
        self.assertTrue(others[0].done())


if __name__ == "__main__":
    unittest.main()
