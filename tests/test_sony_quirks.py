import unittest
from unittest import mock

from tests import support

from camera import sony

A7IV_MODEL = "Sony Alpha-A7 IV (PC Control)"


class QuirkMatching(unittest.TestCase):
    def test_no_model_returns_none(self):
        self.assertIsNone(sony.quirks(None))
        self.assertIsNone(sony.quirks(""))

    def test_other_vendors_are_declined(self):
        for model in ("Canon EOS R5", "Nikon Z 6_2", "USB camera (gphoto2)"):
            with self.subTest(model=model):
                self.assertIsNone(sony.quirks(model))

    def test_matches_the_real_a7iv_model_string(self):
        self.assertIsNotNone(sony.quirks(A7IV_MODEL))

    def test_vendor_match_is_case_insensitive(self):
        for model in ("SONY ALPHA-A7 IV", "sony alpha-a7 iv", "Sony Alpha-A7 IV"):
            with self.subTest(model=model):
                self.assertIsNotNone(sony.quirks(model))

    def test_unknown_sony_body_still_gets_the_general_table(self):
        self.assertEqual(sony.quirks("Sony Alpha-A1 II (PC Control)"), sony.GENERAL)


class QuirkContents(unittest.TestCase):
    def test_a7iv_focus_widgets(self):
        quirks = sony.quirks(A7IV_MODEL)
        self.assertEqual(quirks["af_widget"], "autofocus")
        self.assertEqual(quirks["manual_focus_widget"], "manualfocus")
        self.assertEqual(quirks["focus_mode_widget"], "focusmode")
        self.assertEqual(quirks["af_drive_values"], (1, 0))
        self.assertIn(quirks["af_target_mode"], quirks["af_modes"])
        self.assertIn(quirks["mf_target_mode"], quirks["mf_modes"])

    def test_result_is_a_copy_not_the_shared_table(self):
        quirks = sony.quirks(A7IV_MODEL)
        quirks["shot_gap"] = 99.0
        self.assertNotEqual(sony.GENERAL["shot_gap"], 99.0)
        self.assertNotEqual(sony.quirks(A7IV_MODEL)["shot_gap"], 99.0)

    def test_model_overrides_layer_over_general(self):
        with mock.patch.dict(sony.MODELS, {"A7 IV": {"shot_gap": 4.25}}, clear=True):
            self.assertEqual(sony.quirks(A7IV_MODEL)["shot_gap"], 4.25)
            self.assertEqual(sony.quirks("Sony Alpha-A9")["shot_gap"],
                             sony.GENERAL["shot_gap"])

    def test_model_key_matching_is_substring_and_case_insensitive(self):
        with mock.patch.dict(sony.MODELS, {"A7 IV": {"shot_gap": 4.25}}, clear=True):
            for model in ("Sony Alpha-A7 IV (Control)",
                          "Sony Alpha-A7 IV (PC Control)",
                          "SONY ALPHA-A7 IV"):
                with self.subTest(model=model):
                    self.assertEqual(sony.quirks(model)["shot_gap"], 4.25)


if __name__ == "__main__":
    unittest.main()
