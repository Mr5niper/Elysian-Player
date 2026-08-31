"""Session and preference persistence."""
import json
import os
from pathlib import Path

from ..config import SETTINGS_FILE

from ..logs import get as _get_logger

log = _get_logger("settings")


DEFAULTS = {
    "volume": 0.8,
    "shuffle": False,
    "repeat": "none",
    "playlist": [],
    "last_path": "",
    "last_position": 0.0,
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
        log.warning("could not read settings, using defaults", exc_info=True)
    return data


def save(data: dict) -> None:
    """Write atomically so a crash mid-write cannot leave a truncated file."""
    payload = {k: data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = Path(str(SETTINGS_FILE) + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception:
        log.error("could not write settings", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
