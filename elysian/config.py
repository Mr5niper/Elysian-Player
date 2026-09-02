"""Application-wide constants."""
import sys
from pathlib import Path

APP_NAME = "Elysian Player"
APP_VERSION = "2.3.0.0"

SETTINGS_FILE = Path.home() / ".elysian_player.json"

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg"}

# What the native engine can actually open today: MP4-family only, per its
# frozen contract. Deliberately narrower than the integration plan's list:
# .mkv joins when the demuxer learns the container, and .m4a/.aac stay out
# of AUDIO_EXTENSIONS because the audio backend (miniaudio) cannot decode
# AAC; both would otherwise be dead rows that fail at play time.
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov"}

MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

COVER_NAMES = (
    "cover.jpg", "folder.jpg", "front.jpg",
    "album.jpg", "cover.png", "folder.png",
)

ART_SIZE = 240  # the CSS #art box; art.py renders at 2x this for HiDPI
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
        base = Path(__file__).parent.parent
    return str(Path(base) / rel)
