"""Session and preference persistence."""
import json
import os
from pathlib import Path

from ..config import SETTINGS_FILE

DEFAULTS = {
    "volume": 0.8,
    "shuffle": False,
    "repeat": "none",
    "discovery": False,
    "lyrics": True,
    "playlist": [],
    "last_path": "",
    "last_position": 0.0,
    "window": [None, None],
}


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        if SETTINGS_FILE.is_file():
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULTS:
                    if key in raw:
                        data[key] = raw[key]
    except Exception:
        pass
    return data


def save(data: dict) -> None:
    """Write atomically so a crash mid-write cannot leave a truncated file."""
    payload = {k: data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = Path(str(SETTINGS_FILE) + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
