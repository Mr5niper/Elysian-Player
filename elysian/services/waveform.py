"""Waveform peaks for the Now Playing display.

Decodes the file once, downsamples to a fixed number of buckets and returns the
peak amplitude of each, normalised to 0..1. Runs on a worker thread because
decoding a whole track takes a moment.
"""
import array

from ..logs import get as _get_logger

log = _get_logger("waveform")


BUCKETS = 72


def peaks_for(path: str, buckets: int = BUCKETS) -> list[float]:
    try:
        import miniaudio

        # Read with Python and decode from memory. miniaudio.decode_file()
        # hands the filename to C fopen(), which on Windows interprets the
        # bytes in the ANSI code page while Python encoded them as UTF-8 -
        # so any non-ASCII filename ("09-Renholdër.mp3") fails to open and
        # that track silently loses its waveform. Python's open() handles
        # Windows unicode paths correctly, and one sequential read is also
        # the friendliest access pattern for a network share.
        with open(path, "rb") as fh:
            data = fh.read()
        decoded = miniaudio.decode(
            data, output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1, sample_rate=8000)
        samples = decoded.samples
    except Exception:
        log.warning("could not decode %s for a waveform", path, exc_info=True)
        return []

    total = len(samples)
    if total == 0:
        return []

    step = max(1, total // buckets)
    out = []
    for start in range(0, step * buckets, step):
        chunk = samples[start:start + step]
        if not len(chunk):
            out.append(0.0)
            continue
        hi = 0
        stride = max(1, len(chunk) // 256)
        for i in range(0, len(chunk), stride):
            v = chunk[i]
            if v < 0:
                v = -v
            if v > hi:
                hi = v
        out.append(hi / 32768.0)

    if not out:
        return []
    ceiling = max(out) or 1.0
    return [round(min(1.0, (v / ceiling) ** 0.75), 4) for v in out]
