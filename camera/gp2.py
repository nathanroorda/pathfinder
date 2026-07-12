"""USB camera backend (libgphoto2 2.5.34): connect() -> Gphoto2Camera.

Composes the actions and settings halves into one camera object. The shared
connection and lock live here so both halves serialize against one camera.
"""
import threading

import gphoto2 as gp

from .gp2_actions import ActionsMixin
from .gp2_settings import SettingsMixin


class Gphoto2Camera(ActionsMixin, SettingsMixin):
    def __init__(self, cam):
        self._cam = cam
        self._lock = threading.Lock()
        self._last_shot = 0.0
        self.model = "USB camera (gphoto2)"


def connect():
    cam = gp.Camera()
    cam.init()
    camera = Gphoto2Camera(cam)
    try:
        camera.model = cam.get_abilities().model or camera.model
    except gp.GPhoto2Error:
        pass
    return camera


def disconnect(camera):
    camera._cam.exit()


if __name__ == "__main__":
    cam = connect()
    print("Connected:", cam.model)
    print("Captured:", cam.capture())
    for s in cam.list_settings():
        print(f"  {s['label']} ({s['type']}): {s['value']}")
    disconnect(cam)