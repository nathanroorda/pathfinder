import importlib
import os
import unittest
from unittest import mock

from tests import support

if support.have("pydantic") and support.have("fastapi"):
    import app.app as app_module
    from pydantic import ValidationError


@support.requires("fastapi", "pydantic")
class MaxBulbSeconds(unittest.TestCase):
    def _resolve(self, raw):
        env = {} if raw is None else {"PATHFINDER_MAX_BULB_SECONDS": raw}
        with mock.patch.dict(os.environ, env, clear=False):
            if raw is None:
                os.environ.pop("PATHFINDER_MAX_BULB_SECONDS", None)
            return app_module._max_bulb_seconds()

    def test_unset_uses_the_default(self):
        self.assertEqual(self._resolve(None), app_module.DEFAULT_MAX_BULB_SECONDS)

    def test_a_valid_override_is_honoured(self):
        self.assertEqual(self._resolve("30"), 30.0)
        self.assertEqual(self._resolve("0.5"), 0.5)

    def test_junk_falls_back_to_the_default_instead_of_failing_to_boot(self):
        for raw in ("", "abc", "10s", "nan", "inf", "-inf", "0", "-1", "-0.5"):
            with self.subTest(raw=raw):
                self.assertEqual(self._resolve(raw),
                                 app_module.DEFAULT_MAX_BULB_SECONDS)

    def test_a_rejected_override_is_logged(self):
        with self.assertLogs("app", level="WARNING") as captured:
            self._resolve("banana")
        self.assertIn("banana", "\n".join(captured.output))

    def test_the_default_is_a_bounded_positive_number(self):
        self.assertGreater(app_module.MAX_BULB_SECONDS, 0)
        self.assertLess(app_module.MAX_BULB_SECONDS, float("inf"))


@support.requires("fastapi", "pydantic")
class BulbExposureModel(unittest.TestCase):
    def test_accepts_an_ordinary_exposure(self):
        self.assertEqual(app_module.BulbExposure(seconds=2.5).seconds, 2.5)

    def test_the_ceiling_is_inclusive(self):
        ceiling = app_module.MAX_BULB_SECONDS
        self.assertEqual(app_module.BulbExposure(seconds=ceiling).seconds, ceiling)

    def test_rejects_anything_past_the_ceiling(self):
        with self.assertRaises(ValidationError):
            app_module.BulbExposure(seconds=app_module.MAX_BULB_SECONDS + 0.1)
        with self.assertRaises(ValidationError):
            app_module.BulbExposure(seconds=1e9)

    def test_rejects_zero_and_negative(self):
        for seconds in (0, -0.0, -1, -1e9):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValidationError):
                    app_module.BulbExposure(seconds=seconds)

    def test_rejects_infinity_and_nan(self):
        for seconds in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValidationError):
                    app_module.BulbExposure(seconds=seconds)

    def test_rejects_non_numeric(self):
        for seconds in ("abc", None, [1], {}):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValidationError):
                    app_module.BulbExposure(seconds=seconds)

    def test_seconds_is_required(self):
        with self.assertRaises(ValidationError):
            app_module.BulbExposure()


@support.requires("fastapi", "pydantic")
class BulbCeilingIsConfigurable(unittest.TestCase):
    def _reload_with(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            if not env:
                os.environ.pop("PATHFINDER_MAX_BULB_SECONDS", None)
            importlib.reload(app_module)

    def setUp(self):
        self.addCleanup(self._reload_with, {})

    def test_env_override_moves_the_model_bound(self):
        self._reload_with({"PATHFINDER_MAX_BULB_SECONDS": "5"})

        self.assertEqual(app_module.MAX_BULB_SECONDS, 5.0)
        self.assertEqual(app_module.BulbExposure(seconds=5).seconds, 5.0)
        with self.assertRaises(ValidationError):
            app_module.BulbExposure(seconds=5.1)


@support.requires("fastapi", "pydantic")
class FocusStepModel(unittest.TestCase):
    def test_accepts_signed_steps(self):
        self.assertEqual(app_module.FocusStep(steps=-3).steps, -3)
        self.assertEqual(app_module.FocusStep(steps=0).steps, 0)

    def test_accepts_a_whole_float(self):
        self.assertEqual(app_module.FocusStep(steps=3.0).steps, 3)

    def test_rejects_a_fractional_step(self):
        with self.assertRaises(ValidationError):
            app_module.FocusStep(steps=1.5)

    def test_rejects_non_numeric(self):
        for steps in ("near", None, [1]):
            with self.subTest(steps=steps):
                with self.assertRaises(ValidationError):
                    app_module.FocusStep(steps=steps)

    def test_accepts_steps_within_the_sanity_bound(self):
        limit = app_module.MAX_FOCUS_STEPS
        self.assertEqual(app_module.FocusStep(steps=limit).steps, limit)
        self.assertEqual(app_module.FocusStep(steps=-limit).steps, -limit)

    def test_rejects_absurd_steps(self):
        limit = app_module.MAX_FOCUS_STEPS
        for steps in (limit + 1, -limit - 1, 10 ** 9):
            with self.subTest(steps=steps):
                with self.assertRaises(ValidationError):
                    app_module.FocusStep(steps=steps)

    def test_the_real_bound_is_the_widget_range_not_this_model(self):
        self.assertEqual(app_module.FocusStep(steps=500).steps, 500)


@support.requires("fastapi", "pydantic")
class AfPointModel(unittest.TestCase):
    def test_accepts_a_normalised_tap(self):
        point = app_module.AfPoint(x=0.25, y=0.75)
        self.assertEqual((point.x, point.y), (0.25, 0.75))

    def test_both_axes_are_required(self):
        with self.assertRaises(ValidationError):
            app_module.AfPoint(x=0.5)

    def test_out_of_range_is_accepted_here_and_clamped_downstream(self):
        self.assertEqual(app_module.AfPoint(x=-5.0, y=99.0).x, -5.0)

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValidationError):
            app_module.AfPoint(x="left", y=0.5)


@support.requires("fastapi", "pydantic")
class MagnificationModel(unittest.TestCase):
    def test_accepts_a_level_label(self):
        self.assertEqual(app_module.Magnification(level="5.5").level, "5.5")

    def test_the_level_is_required(self):
        with self.assertRaises(ValidationError):
            app_module.Magnification()

    def test_the_real_bound_is_the_widgets_choices_not_this_model(self):
        self.assertEqual(app_module.Magnification(level="99").level, "99")

    def test_rejects_null_and_containers(self):
        for level in (None, ["Off"]):
            with self.subTest(level=level):
                with self.assertRaises(ValidationError):
                    app_module.Magnification(level=level)


@support.requires("fastapi", "pydantic")
class SettingValueModel(unittest.TestCase):
    def test_preserves_the_json_type(self):
        for value, expected in [("800", str), (800, int), (2.8, float), (True, bool)]:
            with self.subTest(value=value):
                self.assertIsInstance(app_module.SettingValue(value=value).value,
                                      expected)

    def test_rejects_null_and_containers(self):
        for value in (None, [1, 2], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    app_module.SettingValue(value=value)

    def test_value_is_required(self):
        with self.assertRaises(ValidationError):
            app_module.SettingValue()


if __name__ == "__main__":
    unittest.main()
