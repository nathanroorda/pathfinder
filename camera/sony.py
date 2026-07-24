GENERAL = {
    "shot_gap": 1.5,
    "capture_retry_attempts": 2,
    "movie_widget": "movie",
    "af_widget": "autofocus",
    "af_drive_values": (1, 0),
    "manual_focus_widget": "manualfocus",
    "focus_mode_widget": "focusmode","af_modes": ("Automatic", "AF-A", "AF-C", "AF-S", "DMF"),
    "af_target_mode": "AF-A",
    "mf_modes": ("Manual",),
    "mf_target_mode": "Manual",
}

MODELS = {
    "A7 IV": {},
}


def quirks(model):
    if not model or "sony" not in model.lower():
        return None
    q = dict(GENERAL)
    for key, overrides in MODELS.items():
        if key.lower() in model.lower():
            q.update(overrides)
            break
    return q
