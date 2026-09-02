"""Shell-facing adapter over the owned media engine.

Layer C of the video architecture: the same surface shape as the audio
PlaybackEngine, so the shell can hold either behind one variable when
integration happens in a later part. No decoding logic lives here; this
class translates engine state into the properties the shell already knows
how to consume, and degrades exactly the way the audio engine does: if the
native library is missing or broken, `available` is False, `error` says why,
and every method is a safe no-op.

Not wired into Api yet, deliberately: the plan integrates the shell only
after the engine behind the frozen ABI is proven.
"""
from .. import logs
from .bindings import (ElyMediaInfo, MEDIA_VIDEO, NativeLib,
                       VideoEngineError, find_library)

log = logs.get("video")


class VideoPlaybackEngine:
    def __init__(self, dll_path: str | None = None):
        self.available = False
        self.error: str | None = None
        self._lib: NativeLib | None = None
        self._handle = None
        self._path: str | None = None
        self._info: ElyMediaInfo | None = None
        try:
            found = find_library(dll_path)
            if not found:
                raise OSError("elysian_video library not found")
            self._lib = NativeLib(found)
            self._handle = self._lib.lib.ely_create_player()
            if not self._handle:
                raise VideoEngineError(1, "could not create player")
            self.available = True
            log.info("video engine loaded from %s", found)
        except Exception as exc:
            # Same posture as the audio engine: the app must still launch.
            self.error = str(exc)
            log.warning("video engine unavailable: %s", exc)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        # Deliberate lifecycle hygiene: unload releases media and pipeline
        # state through the contract's own path before the handle dies, so
        # a future real engine tears down decoders in its defined order
        # rather than relying on destroy to imply it.
        if self._lib and self._handle:
            self._lib.lib.ely_unload(self._handle)
            self._lib.lib.ely_destroy_player(self._handle)
            self._handle = None
            self._path = None
            self._info = None

    # -- loading and transport ------------------------------------------------

    def load(self, path: str) -> None:
        if not self.available:
            return
        self._lib.check(self._lib.lib.ely_load(self._handle, path),
                        self._handle)
        self._path = path
        self._info = self._read_info()

    def play(self, path: str, start: float = 0.0) -> None:
        """Load if needed, then start; mirrors the audio engine's play()."""
        if not self.available:
            return
        if path != self._path:
            self.load(path)
        self._lib.check(self._lib.lib.ely_play(self._handle), self._handle)
        if start > 0.0:
            self.seek(start)

    def pause(self) -> None:
        if self.available and self.playing and not self.paused:
            self._lib.check(self._lib.lib.ely_pause(self._handle),
                            self._handle)

    def resume(self) -> None:
        if self.available and self.paused:
            self._lib.check(self._lib.lib.ely_resume(self._handle),
                            self._handle)

    def toggle(self) -> None:
        if not self.available:
            return
        if self.paused:
            self.resume()
        elif self.playing:
            self.pause()

    def stop(self) -> None:
        # Legal from any loaded state, not just active: the contract has
        # only EMPTY reject stop. _path deliberately stays set afterwards,
        # because stop keeps the media loaded; do not "clean it up".
        if self.available and self._path is not None:
            self._lib.check(self._lib.lib.ely_stop(self._handle),
                            self._handle)

    def seek(self, seconds: float) -> None:
        if self.available and self._path is not None:
            self._lib.check(
                self._lib.lib.ely_seek(self._handle, float(seconds)),
                self._handle)

    def nudge(self, delta: float) -> None:
        self.seek(self.position + float(delta))

    def finished(self) -> bool:
        if not self.available:
            return False
        return bool(self._lib.lib.ely_is_finished(self._handle))

    # -- volume ----------------------------------------------------------------

    def set_volume(self, value: float) -> None:
        if self.available:
            self._lib.check(
                self._lib.lib.ely_set_volume(self._handle, float(value)),
                self._handle)

    @property
    def volume(self) -> float:
        if not self.available:
            return 0.0
        return float(self._lib.lib.ely_get_volume(self._handle))

    # -- state ------------------------------------------------------------------

    @property
    def playing(self) -> bool:
        return self.available and bool(
            self._lib.lib.ely_is_playing(self._handle))

    @property
    def paused(self) -> bool:
        return self.available and bool(
            self._lib.lib.ely_is_paused(self._handle))

    @property
    def active(self) -> bool:
        return self.available and bool(
            self._lib.lib.ely_is_active(self._handle))

    @property
    def position(self) -> float:
        if not self.available:
            return 0.0
        return float(self._lib.lib.ely_get_position(self._handle))

    @property
    def duration(self) -> float:
        if not self.available:
            return 0.0
        return float(self._lib.lib.ely_get_duration(self._handle))

    # -- media geometry, for the shell's future video pane ----------------------

    @property
    def has_video(self) -> bool:
        return bool(self._info and self._info.has_video)

    @property
    def width(self) -> int:
        return int(self._info.width) if self._info else 0

    @property
    def height(self) -> int:
        return int(self._info.height) if self._info else 0

    @property
    def kind(self) -> int:
        return int(self._info.kind) if self._info else 0

    def is_video(self) -> bool:
        return self.kind == MEDIA_VIDEO

    # -- video target -------------------------------------------------------------

    def set_video_target(self, hwnd: int) -> None:
        if self.available:
            self._lib.check(
                self._lib.lib.ely_set_video_hwnd(self._handle,
                                                 hwnd if hwnd else None),
                self._handle)

    def resize_video(self, width: int, height: int) -> None:
        if self.available:
            self._lib.check(
                self._lib.lib.ely_resize_video(self._handle, int(width),
                                               int(height)),
                self._handle)

    # -- internal -------------------------------------------------------------------

    def _read_info(self) -> ElyMediaInfo:
        import ctypes
        info = ElyMediaInfo()
        info.struct_size = ctypes.sizeof(ElyMediaInfo)
        self._lib.check(
            self._lib.lib.ely_get_media_info(self._handle,
                                             ctypes.byref(info)),
            self._handle)
        return info
