"""On-demand spectrum and waveform data for the Now Playing visualizer.

Unlike waveform.py's peaks_for, which precomputes one amplitude value per
bucket for the *entire* track up front (there is only ever one such array,
worth keeping around for as long as the track plays), this decodes the track
once into a cached PCM buffer and then computes a single FFT window *per
call*, for whatever position is currently playing. Precomputing a spectrum
for every frame of a whole track the same way peaks_for does would mean one
FFT per frame - thousands of them - before the visualizer could show
anything, for a feature the person may never even open. A single FFT here
measures low single-digit milliseconds even in pure Python (no numpy - this
project already keeps to a short, deliberate dependency list, and a plain
radix-2 FFT is comfortably fast enough at these window sizes), so computing
one per poll while the visualizer is actually open is cheap enough to just
do live instead.
"""
import array
import cmath
import math

from ..logs import get as _get_logger

log = _get_logger("visualizer")

SAMPLE_RATE = 22050   # Nyquist 11025Hz - covers what a bar display needs;
                      # higher would cost more per FFT for bars nobody sees.
WINDOW = 2048         # ~93ms of audio per spectrum frame at SAMPLE_RATE.
BARS = 32
WAVE_POINTS = 64


def _hann(n: int) -> list[float]:
    if n <= 1:
        return [1.0] * n
    return [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]


_HANN_CACHE: dict[int, list[float]] = {}


def _hann_window(n: int) -> list[float]:
    win = _HANN_CACHE.get(n)
    if win is None:
        win = _hann(n)
        _HANN_CACHE[n] = win
    return win


def _fft(a: list[complex]) -> list[complex]:
    """Iterative radix-2 Cooley-Tukey. Caller pads to a power of two."""
    n = len(a)
    if n <= 1:
        return a
    a = a[:]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        ang = -2 * math.pi / length
        wlen = complex(math.cos(ang), math.sin(ang))
        for i in range(0, n, length):
            w = 1 + 0j
            half = length // 2
            for k in range(i, i + half):
                u = a[k]
                v = a[k + half] * w
                a[k] = u + v
                a[k + half] = u - v
                w *= wlen
        length <<= 1
    return a


def _bar_edges(bars: int, max_bin: int) -> list[int]:
    """Log-spaced bin boundaries, bin 1 to max_bin.

    Equal *ratios* rather than equal differences between edges, the same
    idea a classic EQ display uses: bass gets many narrow bars, treble gets
    fewer, wide ones, matching how music actually distributes energy and how
    the ear actually resolves pitch.
    """
    if max_bin < bars:
        return list(range(1, max_bin + 1)) + [max_bin] * (bars - max_bin + 1)
    log_max = math.log(max_bin)
    edges = [1]
    for i in range(1, bars + 1):
        edge = int(round(math.exp((i / bars) * log_max)))
        edges.append(max(edge, edges[-1] + 1))
    return edges


class VisualizerProvider:
    def __init__(self):
        self._path: str | None = None
        self._samples: array.array | None = None

    def ensure_decoded(self, path: str) -> bool:
        """Decode and cache path's PCM if not already cached. True on
        success (including "already cached"); False if decoding failed."""
        if self._path == path and self._samples is not None:
            return True
        try:
            import miniaudio

            # Same reasoning as waveform.py: Python's own open() handles a
            # non-ASCII Windows path correctly where handing the filename
            # to miniaudio's C decoder does not.
            with open(path, "rb") as fh:
                data = fh.read()
            decoded = miniaudio.decode(
                data, output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1, sample_rate=SAMPLE_RATE)
            self._samples = decoded.samples
            self._path = path
            return True
        except Exception:
            log.warning("could not decode %s for the visualizer", path,
                        exc_info=True)
            self._path = None
            self._samples = None
            return False

    def frame_at(self, path: str, position_seconds: float,
                 bars: int = BARS, window: int = WINDOW,
                 wave_points: int = WAVE_POINTS) -> dict:
        if self._path != path or self._samples is None:
            return {"bars": [0.0] * bars, "wave": [0.0] * wave_points}

        samples = self._samples
        total = len(samples)
        if total == 0:
            return {"bars": [0.0] * bars, "wave": [0.0] * wave_points}

        center = int(position_seconds * SAMPLE_RATE)
        start = center - window // 2

        # Pad with zeros at the clip's start/end rather than shrinking the
        # window there - a shorter window would change the frequency
        # resolution right at the edges, showing up as the bars behaving
        # differently for the first/last fraction of a second of a track.
        hann = _hann_window(window)
        windowed = [0j] * window
        for i in range(window):
            idx = start + i
            if 0 <= idx < total:
                windowed[i] = complex(samples[idx] / 32768.0 * hann[i], 0.0)

        spectrum = _fft(windowed)
        max_bin = window // 2
        magnitudes = [abs(spectrum[i]) for i in range(1, max_bin + 1)]

        edges = _bar_edges(bars, max_bin)
        bar_values = []
        for b in range(bars):
            lo, hi = edges[b], edges[b + 1]
            group = magnitudes[lo - 1:hi - 1] or [0.0]
            # Peak, not average: a real bucket at higher frequencies can
            # span dozens of bins, and music energy concentrates in
            # specific notes and harmonics rather than spreading evenly
            # across a band - averaging dilutes a strong, narrow peak
            # into nothing once the bucket is wide enough. Confirmed
            # directly against a pure tone whose entire energy sat in a
            # single bin: averaged, it vanished below the noise floor;
            # peak, it reads correctly.
            peak = max(group)
            db = 20 * math.log10(peak / window + 1e-9)
            bar_values.append(round(_clamp((db + 60) / 60, 0.0, 1.0), 4))

        wave_values = []
        wave_step = max(1, window // wave_points)
        for i in range(wave_points):
            idx = i * wave_step
            if idx < window:
                s = start + idx
                v = samples[s] / 32768.0 if 0 <= s < total else 0.0
            else:
                v = 0.0
            wave_values.append(round(_clamp(v, -1.0, 1.0), 4))

        return {"bars": bar_values, "wave": wave_values}


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v
