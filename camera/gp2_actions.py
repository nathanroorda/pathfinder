"""Camera actions: commands that make the camera DO something.

An "action" fires the shutter, downloads a frame, drives autofocus, etc. — as
opposed to settings, which are values. Actions are invoked deliberately by the
app, never surfaced as generic toggles, because triggering some of them
(e.g. autofocus) through the config tree can crash the driver.
"""
import os
import time

import gphoto2 as gp

CAPTURE_DIR = os.environ.get("PATHFINDER_CAPTURE_DIR", "captures")
MIN_SHOT_GAP = 1.5          # seconds the a7 IV needs to settle between shots


class ActionsMixin:
    """Adds camera actions to a camera that provides self._cam / self._lock."""

    def capture(self, save_dir=CAPTURE_DIR):
        """Fire the shutter, download the image to the Pi, return its path."""
        with self._lock:
            wait = MIN_SHOT_GAP - (time.monotonic() - self._last_shot)
            if wait > 0:
                time.sleep(wait)
            self._drain_events()           # clear the previous shot's events
            path = self._capture_with_retry()
            os.makedirs(save_dir, exist_ok=True)
            target = os.path.join(save_dir, f"{int(time.time())}_{path.name}")
            self._cam.file_get(
                path.folder, path.name, gp.GP_FILE_TYPE_NORMAL).save(target)
            self._last_shot = time.monotonic()
            return target

    def _drain_events(self, timeout_ms=200):
        try:
            while self._cam.wait_for_event(timeout_ms)[0] != gp.GP_EVENT_TIMEOUT:
                pass
        except gp.GPhoto2Error:
            pass

    def _capture_with_retry(self, attempts=2):
        # The a7 IV sometimes returns -1 on the shot right after a previous one
        # even though the shutter fired; a short wait + retry succeeds.
        for i in range(attempts):
            try:
                return self._cam.capture(gp.GP_CAPTURE_IMAGE)
            except gp.GPhoto2Error as exc:
                if exc.code == gp.GP_ERROR and i < attempts - 1:
                    time.sleep(1.0)
                    self._drain_events()
                    continue
                raise