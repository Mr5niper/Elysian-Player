"""Audio playback built on just_playback (miniaudio).

This replaces the pygame.mixer engine and removes four v1 defects outright:

  * seek() used to call play() then set_pos(), restarting the stream from zero
    and producing an audible blip. Here seek is a real decoder seek.
  * position was tracked with time.monotonic() and drifted away from the audio.
    curr_pos is reported by the decoder itself.
  * end-of-track was inferred from `length - 0.4`, clipping the last fraction
    of every track. `active` goes False when the stream genuinely ends.
  * mixer.init() was unguarded, so a machine with no audio device crashed at
    startup with no window to show the traceback in. Initialisation failure is
    captured and surfaced instead.
"""


class PlaybackError(RuntimeError):
    pass


class PlaybackEngine:
    def __init__(self):
        self._backend = None
        self._path: str | None = None
        self._volume = 0.8
        self.available = False
        self.error: str | None = None
        try:
            from just_playback import Playback

            self._backend = Playback()
            self._backend.set_volume(self._volume)
            self.available = True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    # ---- state --------------------------------------------------------

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def playing(self) -> bool:
        return bool(self._backend and self._backend.playing)

    @property
    def paused(self) -> bool:
        return bool(self._backend and self._backend.paused)

    @property
    def active(self) -> bool:
        """True while a stream is loaded and has not reached its end."""
        return bool(self._backend and self._backend.active)

    @property
    def position(self) -> float:
        if not self._backend or not self._backend.active:
            return 0.0
        try:
            return float(self._backend.curr_pos)
        except Exception:
            return 0.0

    @property
    def duration(self) -> float:
        if not self._backend or self._path is None:
            return 0.0
        try:
            return float(self._backend.duration)
        except Exception:
            return 0.0

    @property
    def volume(self) -> float:
        return self._volume

    # ---- transport ----------------------------------------------------

    def load(self, path: str) -> None:
        if not self._backend:
            raise PlaybackError(self.error or "no audio device")
        try:
            self._backend.load_file(path)
        except Exception as exc:
            self._path = None
            raise PlaybackError(str(exc)) from exc
        self._path = path
        self._backend.set_volume(self._volume)

    def play(self, path: str, start: float = 0.0) -> None:
        self.load(path)
        self._backend.play()
        if start > 0:
            self.seek(start)

    def pause(self) -> None:
        if self._backend and self._backend.playing:
            self._backend.pause()

    def resume(self) -> None:
        if self._backend and self._backend.paused:
            self._backend.resume()

    def toggle(self) -> None:
        if self.paused:
            self.resume()
        elif self.playing:
            self.pause()

    def stop(self) -> None:
        if self._backend:
            try:
                self._backend.stop()
            except Exception:
                pass
        self._path = None

    def seek(self, seconds: float) -> None:
        if not self._backend or not self._backend.active:
            return
        target = max(0.0, min(float(seconds), max(0.0, self.duration - 0.05)))
        try:
            self._backend.seek(target)
        except Exception:
            pass

    def nudge(self, delta: float) -> None:
        if self.active:
            self.seek(self.position + delta)

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, float(value)))
        if self._backend:
            try:
                self._backend.set_volume(self._volume)
            except Exception:
                pass

    def finished(self) -> bool:
        """True once a loaded track has run to its end."""
        return self._path is not None and not self.active
