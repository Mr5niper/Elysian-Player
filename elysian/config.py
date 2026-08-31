"""Application-wide constants."""
import os
import sys
from pathlib import Path

APP_NAME = "Elysian Player"
APP_VERSION = "2.0.0.0"

SETTINGS_FILE = Path.home() / ".elysian_player.json"

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg"}

COVER_NAMES = (
    "cover.jpg", "folder.jpg", "front.jpg",
    "album.jpg", "cover.png", "folder.png",
)

ART_SIZE = 160
ART_CACHE_LIMIT = 64

TICK_SECONDS = 0.1

WINDOW_WIDTH = 940
WINDOW_HEIGHT = 580
MIN_WIDTH = 700
MIN_HEIGHT = 420


def resource_path(rel: str) -> str:
    """Resolve a bundled resource, working both in dev and under PyInstaller."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base, rel)
