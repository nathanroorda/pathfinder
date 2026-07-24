GENERAL = {
    "shot_gap": 1.5,
    "capture_retry_attempts": 2,
    "movie_widget": "movie",
    "af_widget": "autofocus",
    # Sony's AF toggle idles at 2; press (1) runs AF, release (0) completes a
    # one-shot without leaving the shutter half-pressed. Overrides the generic
    # single-1 default in gp2.DEFAULT_QUIRKS.
    "af_drive_values": (1, 0),
    "manual_focus_widget": "manualfocus",
    "focus_mode_widget": "focusmode",
    # Modes in which each action actually reaches the motor. If the body is
    # already in one of these, the button leaves it alone; otherwise it switches
    # to the target. "Manual" is the only mode where AF is dead; "Manual" is the
    # only mode we're *sure* the manual-focus drive moves the motor (DMF may work
    # too — add it to `mf_modes` once verified on the body).
    "af_modes": ("Automatic", "AF-A", "AF-C", "AF-S", "DMF"),
    "af_target_mode": "AF-A",
    "mf_modes": ("Manual",),
    "mf_target_mode": "Manual",
}

MODELS = {
    # Per-model overrides, keyed by a distinctive substring of the gphoto2
    # abilities model string — NOT the USB/internal "ILCE-7M4" name. gphoto2
    # reports e.g. "Sony Alpha-A7 IV (PC Control)", so match on "A7 IV". An empty
    # dict means "use GENERAL unchanged" (the α7 IV needs no overrides).
    "A7 IV": {},
}


def quirks(model):
    # Any Sony body gets the GENERAL defaults; a matching MODELS entry layers its
    # overrides on top. Substring matching (not equality) tolerates the varying
    # suffixes gphoto2 appends — "(Control)", "(PC Control)", firmware revisions.
    if not model or "sony" not in model.lower():
        return None
    q = dict(GENERAL)
    for key, overrides in MODELS.items():
        if key.lower() in model.lower():
            q.update(overrides)
            break
    return q
