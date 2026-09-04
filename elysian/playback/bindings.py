"""Low-level ctypes bindings for the owned media engine (elysian_video).

Layer B of the video architecture: loads the library, declares the ABI, maps
result codes to exceptions, and nothing else. No shell logic, no state. The
shell-facing adapter lives in video_engine.py.

The library is found, in order: an explicit path argument, the
ELYSIAN_VIDEO_DLL environment variable, then default names next to the
frozen executable / repo root (elysian_video.dll on Windows,
libelysian_video.so elsewhere, the latter existing purely so the contract
can be tested off-Windows).
"""
import ctypes
import os
import sys
from pathlib import Path

#: The ABI generation this binding understands. ely_abi_version() must match.
ABI_VERSION = 1

RESULT_NAMES = {
    0: "OK",
    1: "GENERIC",
    2: "BAD_ARG",
    3: "NOT_FOUND",
    4: "UNSUPPORTED",
    5: "BAD_CONTAINER",
    6: "BAD_STREAM",
    7: "DECODE",
    8: "AUDIO_DEVICE",
    9: "VIDEO_TARGET",
    10: "SEEK",
    11: "BAD_STATE",
}

STATE_NAMES = {
    0: "EMPTY", 1: "LOADED", 2: "PLAYING", 3: "PAUSED",
    4: "STOPPED", 5: "ENDED", 6: "ERROR",
}

MEDIA_UNKNOWN, MEDIA_AUDIO, MEDIA_VIDEO = 0, 1, 2


class VideoEngineError(Exception):
    """A failing engine call. .code is the ElyResult, .name its label."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.name = RESULT_NAMES.get(code, str(code))
        super().__init__(f"[{self.name}] {message}" if message else
                         f"[{self.name}]")


class ElyMediaInfo(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_int),
        ("has_audio", ctypes.c_int),
        ("has_video", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("duration", ctypes.c_double),
        ("frame_rate", ctypes.c_double),
        ("audio_sample_rate", ctypes.c_int),
        ("audio_channels", ctypes.c_int),
        ("kind", ctypes.c_int),
    ]


def _default_candidates() -> list:
    # The real engine builds as elysian_video_real; the stub keeps the
    # original name. Preferring the real one means a repo with both picks
    # the engine that actually plays.
    names = (["elysian_video_real.dll", "elysian_video.dll"]
             if os.name == "nt"
             else ["libelysian_video_real.so", "libelysian_video.so",
                   "elysian_video.so"])
    roots = []
    if getattr(sys, "frozen", False):
        # PyInstaller's onefile bootloader extracts everything embedded
        # with --add-binary (this DLL and its FFmpeg runtime DLLs) to a
        # temp directory named in sys._MEIPASS, NOT next to sys.executable
        # - that path is only the exe's own location, which in onefile
        # mode holds nothing but the bootloader stub itself. Checked first
        # since it is the one onefile actually populates; sys.executable's
        # parent stays as a fallback for a onedir build or a DLL placed by
        # hand next to the exe during development.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).parent)
    here = Path(__file__).resolve()
    roots.append(here.parents[2])                       # repo root
    roots.append(here.parents[2] / "native" / "elysian_video")
    return [root / name for root in roots for name in names]


def find_library(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    env = os.environ.get("ELYSIAN_VIDEO_DLL", "")
    if env and Path(env).is_file():
        return env
    for candidate in _default_candidates():
        if candidate.is_file():
            return str(candidate)
    return None


def _add_dll_search_dirs(lib_path: str) -> None:
    """Make elysian_video_real.dll's own dependencies findable.

    Since Python 3.8, ctypes' DLL loader on Windows no longer implicitly
    searches a loaded DLL's own directory for its dependencies (Windows'
    own "safe DLL search mode" hardening); os.add_dll_directory() is the
    replacement, and it has to be called before the WinDLL() call below,
    not after.

    Two directories are added, covering both places the FFmpeg runtime
    DLLs can legitimately be: next to the engine DLL itself (true for a
    frozen build, since PyInstaller's onefile bootloader extracts
    elysian_video_real.dll and its FFmpeg runtime DLLs into the same
    _MEIPASS temp directory), and third_party/ffmpeg/bin at the repo root
    (true when running from source, since nothing else ever copies the
    fetched FFmpeg build's DLLs anywhere near the engine DLL - BUILD_EXE.bat
    only ever embeds them into the frozen exe). Missing directories and
    Windows' own rejection of a bad path are both non-fatal: the
    subsequent WinDLL() call surfaces its own real error either way, which
    is what actually matters to the caller.
    """
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    candidates = [Path(lib_path).resolve().parent]
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "third_party" / "ffmpeg" / "bin")
    for d in candidates:
        try:
            if d.is_dir():
                os.add_dll_directory(str(d))
        except OSError:
            pass


class NativeLib:
    """Owns the loaded library and the raw call surface.

    Raises OSError if the library cannot be loaded and VideoEngineError if
    its ABI version is not one this binding understands.
    """

    def __init__(self, lib_path: str):
        _add_dll_search_dirs(lib_path)
        loader = ctypes.WinDLL if os.name == "nt" else ctypes.CDLL
        self.path = lib_path
        self.lib = loader(lib_path)
        self._declare()
        got = self.lib.ely_abi_version()
        if got != ABI_VERSION:
            raise VideoEngineError(
                1, f"engine ABI v{got}, bindings expect v{ABI_VERSION}")

    def _declare(self) -> None:
        L = self.lib
        L.ely_abi_version.argtypes = []
        L.ely_abi_version.restype = ctypes.c_int

        L.ely_create_player.argtypes = []
        L.ely_create_player.restype = ctypes.c_void_p
        L.ely_destroy_player.argtypes = [ctypes.c_void_p]
        L.ely_destroy_player.restype = None

        L.ely_load.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        L.ely_load.restype = ctypes.c_int
        L.ely_unload.argtypes = [ctypes.c_void_p]
        L.ely_unload.restype = ctypes.c_int

        for name in ("ely_play", "ely_pause", "ely_resume", "ely_stop"):
            fn = getattr(L, name)
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_int

        L.ely_seek.argtypes = [ctypes.c_void_p, ctypes.c_double]
        L.ely_seek.restype = ctypes.c_int

        L.ely_set_volume.argtypes = [ctypes.c_void_p, ctypes.c_float]
        L.ely_set_volume.restype = ctypes.c_int
        L.ely_get_volume.argtypes = [ctypes.c_void_p]
        L.ely_get_volume.restype = ctypes.c_float

        L.ely_get_position.argtypes = [ctypes.c_void_p]
        L.ely_get_position.restype = ctypes.c_double
        L.ely_get_duration.argtypes = [ctypes.c_void_p]
        L.ely_get_duration.restype = ctypes.c_double

        for name in ("ely_is_playing", "ely_is_paused", "ely_is_active",
                     "ely_is_finished", "ely_get_state"):
            fn = getattr(L, name)
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_int

        L.ely_get_media_info.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ElyMediaInfo)]
        L.ely_get_media_info.restype = ctypes.c_int

        L.ely_set_video_hwnd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.ely_set_video_hwnd.restype = ctypes.c_int
        L.ely_resize_video.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_int]
        L.ely_resize_video.restype = ctypes.c_int

        L.ely_get_last_error.argtypes = [ctypes.c_void_p]
        L.ely_get_last_error.restype = ctypes.c_wchar_p

    # -- helpers the wrapper builds on --------------------------------------

    def check(self, code: int, handle) -> None:
        """Raise VideoEngineError for a nonzero ElyResult."""
        if code == 0:
            return
        message = ""
        if handle:
            message = self.lib.ely_get_last_error(handle) or ""
        raise VideoEngineError(code, message)
