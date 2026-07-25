import importlib
import importlib.util
import logging
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    REAL_GPHOTO2 = importlib.import_module("gphoto2")  # captured before shadowing
except Exception:
    REAL_GPHOTO2 = None

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.chdir(REPO_ROOT)  # app.py's StaticFiles(directory="web") resolves against cwd

from .fakes import fake_gphoto2  # noqa: E402

sys.modules["gphoto2"] = fake_gphoto2  # must precede any import of camera

logging.getLogger().addHandler(logging.NullHandler())  # silence logging's lastResort


def have(module_name):
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def requires(*module_names):
    missing = [name for name in module_names if not have(name)]
    return unittest.skipIf(
        missing, f"requires {', '.join(missing)} (install with requirements.txt)")


class FakeClock:
    def __init__(self, monotonic=1_000_000.0, wall=1_774_000_000.0):
        self._monotonic = monotonic
        self._wall = wall
        self.sleeps = []

    def monotonic(self):
        return self._monotonic

    def time(self):
        return self._wall

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds):
        self._monotonic += seconds
        self._wall += seconds

    @property
    def slept(self):
        return sum(self.sleeps)
