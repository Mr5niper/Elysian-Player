"""Shell-side hybrid media controller.

Keeps the existing audio PlaybackEngine for audio files and routes video
files to VideoPlaybackEngine, presenting the same surface shape Api already
consumes. Two deliberate deviations from the integration plan, both because
the engines behave differently when broken:

  * play() raises PlaybackError when the target engine is unavailable. The
    video engine's methods are silent no-ops without its library, and the
    plan's controller would have reported a play that never happened: the
    row would highlight, nothing would move, and no status would say why.
  * volume is owned here as a single mirrored value. Reading it from the
    active engine meant a missing video library reported 0.0 and yanked the
    slider to zero the moment a video was tried.
"""
from .engine import PlaybackEngine, PlaybackError
from .video_engine import VideoPlaybackEngine
from ..media import is_video_path


class MediaController:
    def __init__(self):
        self._audio = PlaybackEngine()
        self._video = VideoPlaybackEngine()
        self._kind = "audio"
        self._volume = self._audio.volume

    @property
    def current_kind(self) -> str:
        return self._kind

    @property
    def active_engine(self):
        return self._video if self._kind == "video" else self._audio

    @property
    def available(self) -> bool:
        # The app is usable if either backend is: a machine with no audio
        # device can still play video, and a missing video library must not
        # report the whole player broken.
        return self._audio.available or self._video.available

    @property
    def error(self):
        return self.active_engine.error

    @property
    def playing(self) -> bool:
        return self.active_engine.playing

    @property
    def paused(self) -> bool:
        return self.active_engine.paused

    @property
    def active(self) -> bool:
        return self.active_engine.active

    @property
    def position(self) -> float:
        return self.active_engine.position

    @property
    def duration(self) -> float:
        return self.active_engine.duration

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def has_video(self) -> bool:
        return self._kind == "video" and bool(
            getattr(self._video, "has_video", False))

    @property
    def width(self) -> int:
        return self._video.width if self._kind == "video" else 0

    @property
    def height(self) -> int:
        return self._video.height if self._kind == "video" else 0

    @property
    def native_state_name(self) -> str:
        """Diagnostic passthrough: the video engine's own ely_get_state(),
        empty string when the current track is not video (nothing native
        to report; the audio engine has no equivalent internal states)."""
        if self._kind != "video":
            return ""
        return getattr(self._video, "native_state_name", "")

    @property
    def native_last_error(self) -> str:
        if self._kind != "video":
            return ""
        return getattr(self._video, "native_last_error", "")

    def set_video_target(self, hwnd: int) -> None:
        self._video.set_video_target(hwnd)

    def resize_video(self, width: int, height: int) -> None:
        self._video.resize_video(width, height)

    def play(self, path: str, start: float = 0.0) -> None:
        self.stop()
        self._kind = "video" if is_video_path(path) else "audio"
        engine = self.active_engine
        if not engine.available:
            raise PlaybackError(engine.error or "engine unavailable")
        try:
            engine.play(path, start)
            engine.set_volume(self._volume)
        except PlaybackError:
            raise
        except Exception as exc:
            raise PlaybackError(str(exc)) from exc

    def pause(self) -> None:
        self.active_engine.pause()

    def resume(self) -> None:
        self.active_engine.resume()

    def toggle(self) -> None:
        self.active_engine.toggle()

    def stop(self) -> None:
        self._audio.stop()
        self._video.stop()

    def seek(self, seconds: float) -> None:
        self.active_engine.seek(seconds)

    def nudge(self, delta: float) -> None:
        self.active_engine.nudge(delta)

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, float(value)))
        self._audio.set_volume(self._volume)
        self._video.set_volume(self._volume)

    def finished(self) -> bool:
        return self.active_engine.finished()
