GENERAL = {
    "shot_gap": 1.5,
    "capture_retry_attempts": 2,
}

MODELS = {
    "ILCE-7M4": {},
}


def quirks(model):
    if model not in MODELS:
        return None
    q = dict(GENERAL)
    q.update(MODELS[model])
    return q
