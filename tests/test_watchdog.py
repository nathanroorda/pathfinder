import asyncio
import contextlib
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from tests import support

from tests.fakes.fake_camera import FakeConnectedCamera

if support.have("pydantic") and support.have("fastapi"):
    import app as app_module

SETUP_SH = os.path.join(support.REPO_ROOT, "setup.sh")


def run(coro):
    return asyncio.run(coro)


@contextlib.contextmanager
def env(**values):
    with mock.patch.dict(os.environ):
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield


@support.requires("fastapi", "pydantic")
class SdNotify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pathfinder-notify-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "notify.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.sock.close)
        self.sock.bind(self.path)
        self.sock.settimeout(5)

    def test_a_message_reaches_the_manager(self):
        with env(NOTIFY_SOCKET=self.path):
            self.assertTrue(app_module._sd_notify("WATCHDOG=1"))

        self.assertEqual(self.sock.recv(64), b"WATCHDOG=1")

    def test_an_abstract_socket_address_is_translated(self):
        name = "\0pathfinder-test-" + uuid.uuid4().hex
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(abstract.close)
        abstract.bind(name)
        abstract.settimeout(5)

        with env(NOTIFY_SOCKET="@" + name[1:]):
            self.assertTrue(app_module._sd_notify("READY=1"))

        self.assertEqual(abstract.recv(64), b"READY=1")

    def test_no_manager_listening_is_a_silent_no_op(self):
        with env(NOTIFY_SOCKET=None):
            self.assertFalse(app_module._sd_notify("WATCHDOG=1"))

    def test_a_dead_socket_is_reported_not_raised(self):
        # The app must not die because the manager went away.
        with env(NOTIFY_SOCKET=os.path.join(self.dir, "nothing-here.sock")):
            self.assertFalse(app_module._sd_notify("WATCHDOG=1"))


@support.requires("fastapi", "pydantic")
class WatchdogInterval(unittest.TestCase):
    def test_the_ping_interval_is_half_the_deadline(self):
        with env(WATCHDOG_USEC="30000000", WATCHDOG_PID=None):
            self.assertEqual(app_module._watchdog_interval(), 15.0)

    def test_no_deadline_means_no_watchdog(self):
        with env(WATCHDOG_USEC=None, WATCHDOG_PID=None):
            self.assertIsNone(app_module._watchdog_interval())

    def test_a_deadline_addressed_to_our_own_pid_is_ours(self):
        with env(WATCHDOG_USEC="30000000", WATCHDOG_PID=str(os.getpid())):
            self.assertEqual(app_module._watchdog_interval(), 15.0)

    def test_a_deadline_inherited_from_another_process_is_ignored(self):
        # WATCHDOG_USEC survives a fork/exec; pinging on someone else's behalf
        # would keep their unit alive.
        with env(WATCHDOG_USEC="30000000", WATCHDOG_PID=str(os.getpid() + 1)):
            self.assertIsNone(app_module._watchdog_interval())

    def test_a_malformed_deadline_is_ignored_and_said_out_loud(self):
        with env(WATCHDOG_USEC="soon", WATCHDOG_PID=None):
            with self.assertLogs("app", level="WARNING") as captured:
                self.assertIsNone(app_module._watchdog_interval())

        self.assertIn("WATCHDOG_USEC", "\n".join(captured.output))

    def test_a_zero_deadline_disables_the_watchdog(self):
        with env(WATCHDOG_USEC="0", WATCHDOG_PID=None):
            self.assertIsNone(app_module._watchdog_interval())


@support.requires("fastapi", "pydantic", "anyio")
class Heartbeat(unittest.TestCase):
    TICK = 0.05

    def setUp(self):
        self.pings = []
        patcher = mock.patch.object(app_module, "_sd_notify", self.pings.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def beat(self, ticks=3, interval=None):
        interval = interval or self.TICK
        task = asyncio.create_task(app_module._watchdog(interval))
        await asyncio.sleep(interval * ticks + interval / 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return task

    def test_a_healthy_process_earns_a_ping_every_interval(self):
        run(self.beat(ticks=3))

        self.assertGreaterEqual(len(self.pings), 2)
        self.assertEqual(set(self.pings), {"WATCHDOG=1"})

    def test_a_wedged_threadpool_withholds_the_ping(self):
        async def never_returns(*args, **kwargs):
            await asyncio.Event().wait()

        with mock.patch.object(app_module, "run_in_threadpool", never_returns):
            run(self.beat(ticks=3))

        self.assertEqual(self.pings, [])

    def test_withholding_the_ping_is_logged_as_an_error(self):
        async def never_returns(*args, **kwargs):
            await asyncio.Event().wait()

        with mock.patch.object(app_module, "run_in_threadpool", never_returns):
            with self.assertLogs("app", level="ERROR") as captured:
                run(self.beat(ticks=2))

        self.assertIn("threadpool", "\n".join(captured.output))

    def test_pinging_resumes_once_the_pool_frees_up(self):
        async def scenario():
            gate = asyncio.Event()

            async def gated(func, *args, **kwargs):
                await gate.wait()
                return func(*args, **kwargs)

            with mock.patch.object(app_module, "run_in_threadpool", gated):
                await self.beat(ticks=2)
                stuck = list(self.pings)
                gate.set()
                await self.beat(ticks=2)
            return stuck, list(self.pings)

        stuck, recovered = run(scenario())

        self.assertEqual(stuck, [])
        self.assertGreaterEqual(len(recovered), 1)

    def test_the_probe_rides_the_same_pool_the_camera_operations_use(self):
        import anyio

        async def scenario():
            from fastapi.concurrency import run_in_threadpool

            limiter = anyio.to_thread.current_default_thread_limiter()
            limiter.total_tokens = 1
            started, release = threading.Event(), threading.Event()

            def hold_the_only_worker():
                started.set()
                release.wait(10)

            hog = asyncio.create_task(run_in_threadpool(hold_the_only_worker))
            try:
                while not started.is_set():         # no sleep-and-hope
                    await asyncio.sleep(0.005)
                await self.beat(ticks=2)
                wedged = list(self.pings)
            finally:
                release.set()
                await hog
            await self.beat(ticks=2)
            return wedged, list(self.pings)

        wedged, recovered = run(scenario())

        self.assertEqual(wedged, [])
        self.assertGreaterEqual(len(recovered), 1)

    def test_the_probe_does_not_outlive_the_heartbeat(self):
        async def scenario():
            async def never_returns(*args, **kwargs):
                await asyncio.Event().wait()

            with mock.patch.object(app_module, "run_in_threadpool", never_returns):
                await self.beat(ticks=1)
            await asyncio.sleep(self.TICK)   # let the cancelled probe unwind
            return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        self.assertEqual(run(scenario()), [])


@support.requires("fastapi", "pydantic")
class LifespanWiring(unittest.TestCase):
    def setUp(self):
        self.cam = FakeConnectedCamera()
        app_module.app.state.camera = None
        app_module._connect_lock = asyncio.Lock()
        self.addCleanup(setattr, app_module.app.state, "camera", None)
        self.pings = []
        patcher = mock.patch.object(app_module, "_sd_notify", self.pings.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _running_tasks(self):
        with mock.patch.object(app_module, "_try_connect",
                               lambda app: setattr(app.state, "camera", self.cam)):
            async with app_module.lifespan(app_module.app):
                return [t for t in asyncio.all_tasks()
                        if t is not asyncio.current_task()]

    def test_the_heartbeat_is_armed_when_systemd_asks_for_one(self):
        with env(WATCHDOG_USEC="30000000", WATCHDOG_PID=None):
            tasks = run(self._running_tasks())

        self.assertEqual(len(tasks), 2)          # camera watcher + heartbeat
        self.assertIn("READY=1", self.pings)

    def test_nothing_is_started_when_no_watchdog_is_configured(self):
        with env(WATCHDOG_USEC=None, WATCHDOG_PID=None):
            tasks = run(self._running_tasks())

        self.assertEqual(len(tasks), 1)          # camera watcher only
        self.assertEqual(self.pings, [])

    def test_the_heartbeat_does_not_outlive_the_app(self):
        with env(WATCHDOG_USEC="30000000", WATCHDOG_PID=None):
            tasks = run(self._running_tasks())

        self.assertTrue(all(t.done() for t in tasks))


class SystemdUnit(unittest.TestCase):
    def setUp(self):
        with open(SETUP_SH, encoding="utf-8") as fh:
            text = fh.read()
        found = re.search(r"<<UNIT\n(.*?)\nUNIT\n", text, re.S)
        self.assertIsNotNone(found, "the systemd unit heredoc has moved")
        self.unit = found.group(1)

    def directive(self, name):
        found = re.search(rf"^{name}=(.*)$", self.unit, re.MULTILINE)
        self.assertIsNotNone(found, f"the unit has no {name}=")
        return found.group(1).strip()

    def test_the_unit_arms_the_watchdog(self):
        self.assertGreater(float(self.directive("WatchdogSec")), 0)

    def test_the_notification_socket_is_opened_to_the_main_process(self):
        self.assertEqual(self.directive("NotifyAccess"), "main")

    def test_a_hung_process_is_restarted_rather_than_left_alive(self):
        self.assertEqual(self.directive("Restart"), "always")

    @support.requires("fastapi", "pydantic")
    def test_the_app_pings_well_inside_the_deadline_the_unit_sets(self):
        deadline = float(self.directive("WatchdogSec"))

        with env(WATCHDOG_USEC=str(int(deadline * 1_000_000)), WATCHDOG_PID=None):
            interval = app_module._watchdog_interval()

        self.assertIsNotNone(interval)
        self.assertLessEqual(interval, deadline / 2)


if __name__ == "__main__":
    unittest.main()
