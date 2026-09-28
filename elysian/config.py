"""Application-wide constants."""
import shutil
import sys
from pathlib import Path

APP_NAME = "Elysian Player"
APP_VERSION = "2.6.1.0"

#: Everything the app writes outside its own install folder lives here -
#: settings, the library index, the art cache, the log, the single-instance
#: token - one folder, not half a dozen separate dot-files loose in the
#: home directory alongside everything else a person keeps there.
APP_DATA_DIR = Path.home() / ".elysian_player"

SETTINGS_FILE = APP_DATA_DIR / "settings.json"
LIBRARY_DB_FILE = APP_DATA_DIR / "library.db"
ART_CACHE_DIR = APP_DATA_DIR / "art_cache"
LOG_FILE = APP_DATA_DIR / "player.log"
INSTANCE_TOKEN_FILE = APP_DATA_DIR / "instance"
INSTANCE_SOCKET_FILE = APP_DATA_DIR / "instance.sock"


def _migrate_legacy_home_files() -> None:
    """Move anything still at the old, scattered dot-file locations into
    APP_DATA_DIR, once.

    Every version through 2.6.1.0 wrote each of these straight into the
    home directory as its own separate dot-file or folder
    (.elysian_player.json, .elysian_library.db, .elysian_art_cache/,
    .elysian_player.log plus its rotated backups, .elysian_player_instance)
    rather than one folder. Updating without this would make the app look
    like it forgot every setting, the whole library index, and the entire
    art cache the moment it started reading from a folder that had never
    existed before.

    Runs at import time, here rather than from anything that only fires
    once the window is up: logs.py, single_instance.py and this module's
    own callers all read these same paths the moment they're imported, so
    anything that ran even slightly later risked one of them creating a
    fresh, empty file at the new location first - which would make the old
    one still sitting there look already handled when it was not, and the
    move below would then refuse to touch it (a existing destination is
    treated as "already migrated", deliberately, so a second launch never
    overwrites anything a first launch already wrote fresh at the new
    location).
    """
    try:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return  # can't create the new folder; nothing safe to do further

    home = Path.home()
    legacy_to_new = {
        home / ".elysian_player.json": SETTINGS_FILE,
        home / ".elysian_library.db": LIBRARY_DB_FILE,
        home / ".elysian_library.db-wal": APP_DATA_DIR / "library.db-wal",
        home / ".elysian_library.db-shm": APP_DATA_DIR / "library.db-shm",
        home / ".elysian_art_cache": ART_CACHE_DIR,
        home / ".elysian_player.log": LOG_FILE,
        home / ".elysian_player.log.1": APP_DATA_DIR / "player.log.1",
        home / ".elysian_player.log.2": APP_DATA_DIR / "player.log.2",
        home / ".elysian_player_instance": INSTANCE_TOKEN_FILE,
        home / ".elysian_player_instance.sock": INSTANCE_SOCKET_FILE,
    }
    for src, dest in legacy_to_new.items():
        # Each move stands alone: one file locked or otherwise unmovable
        # (a log file another process still has open, say) must not stop
        # the rest - settings and the library index matter far more than
        # a log that will just start a fresh one at the new location.
        try:
            if src.exists() and not dest.exists():
                shutil.move(str(src), str(dest))
        except OSError:
            pass


_migrate_legacy_home_files()

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg"}

COVER_NAMES = (
    "cover.jpg", "folder.jpg", "front.jpg",
    "album.jpg", "cover.png", "folder.png",
)

ART_SIZE = 240  # the CSS #art box; art.py renders at 2x this for HiDPI
ART_CACHE_LIMIT = 64
EMBED_ART_SIZE = 500  # square dimension for art written into files

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
