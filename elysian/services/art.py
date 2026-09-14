"""Album art extraction.

Returns art as a base64 data URL for the web frontend. The float-array
texture path that DearPyGui needed is gone along with that interface.
"""
import hashlib
import os
import threading
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

from ..config import ART_CACHE_DIR, ART_CACHE_LIMIT, ART_SIZE, COVER_NAMES, \
    EMBED_ART_SIZE
from ..logs import get as _get_logger

log = _get_logger("art")


class ArtProvider:
    def __init__(self, size: int = ART_SIZE, limit: int = ART_CACHE_LIMIT):
        self.size = size
        self.limit = limit
        self._urls: OrderedDict[str, str | None] = OrderedDict()
        # Guards _urls only. More than one caller can share a single
        # ArtProvider instance (the library's background art workers all
        # do), and an OrderedDict mutated from more than one thread at
        # once can raise mid-update - which _build_url's own broad
        # except then quietly records as "this file has no art",
        # permanently, since nothing retries a recorded miss outside of
        # a rescan. The lock only wraps the dict bookkeeping below, never
        # the file read/decode in _build_url, so concurrent lookups for
        # different files still proceed in parallel.
        self._lock = threading.Lock()

    def data_url(self, audio_path: str) -> str | None:
        """Return embedded art as a base64 data URL for the web frontend."""
        with self._lock:
            if audio_path in self._urls:
                self._urls.move_to_end(audio_path)
                return self._urls[audio_path]
        url = self._build_url(audio_path)
        with self._lock:
            self._urls[audio_path] = url
            while len(self._urls) > self.limit:
                self._urls.popitem(last=False)
        return url

    def _build_url(self, audio_path: str) -> str | None:
        jpeg_bytes, _source, _mtime = self.resolve([audio_path])
        if jpeg_bytes is None:
            return None
        import base64
        return ("data:image/jpeg;base64," +
                base64.b64encode(jpeg_bytes).decode("ascii"))

    def resolve(self, candidate_paths):
        """Find and decode the first candidate that actually has art.

        Tries each path's own embedded art first, then that path's
        folder for a cover image file, in the order given (callers pass
        an album's tracks in disc/track order, so the opening track is
        tried first).

        Returns (jpeg_bytes, source_path, source_mtime). source_path is
        whichever file actually supplied the image - the audio file
        itself if the art was embedded, or the specific cover image
        file if it came from the folder - never just "the folder", so
        a caller can later detect either kind of change by checking
        that one file's mtime. Returns (None, None, None) if nothing
        was found in any candidate.
        """
        from PIL import Image

        for path in candidate_paths:
            img = None
            source = None
            raw = self._embedded_bytes(path)
            if raw:
                try:
                    img = Image.open(BytesIO(raw))
                    source = path
                except Exception:
                    log.debug("embedded artwork could not be decoded for %s",
                              path, exc_info=True)
                    img = None
            if img is None:
                folder = Path(path).parent
                for name in COVER_NAMES:
                    candidate = folder / name
                    try:
                        if candidate.is_file():
                            img = Image.open(candidate)
                            source = str(candidate)
                            break
                    except Exception:
                        log.debug("cover file %s could not be opened",
                                  candidate, exc_info=True)
                        continue
            if img is None:
                continue
            try:
                img = img.convert("RGB")
                img.thumbnail((self.size * 2, self.size * 2), Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=86)
                jpeg_bytes = buf.getvalue()
            except Exception:
                log.debug("artwork conversion failed for %s", path,
                          exc_info=True)
                continue
            try:
                mtime = os.path.getmtime(source)
            except OSError:
                # The file that just supplied this image vanished between
                # finding it and stat-ing it - try the next candidate
                # rather than caching an entry with no valid mtime.
                continue
            return jpeg_bytes, source, mtime
        return None, None, None

    def resolve_cached(self, key: str, candidate_paths, get_entry, set_entry):
        """Resolve one album's cover, backed by a persistent on-disk cache.

        get_entry(key) -> dict|None and set_entry(key, source_path,
        source_mtime, thumb_file) -> None are supplied by the caller
        (the library's own get_art_cache_entry/set_art_cache_entry), so
        this stays decoupled from how or where that bookkeeping lives.

        A cache hit whose source file's mtime still matches is just a
        stat() plus a small local file read - no network touch, no
        decode. Anything else (no entry yet, the file's mtime changed
        since it was cached, or the cached thumbnail went missing) falls
        through to resolve(), the only place actual file decoding
        happens, and persists the fresh result for next time.
        """
        import base64

        entry = None
        try:
            entry = get_entry(key)
        except Exception:
            log.warning("art cache lookup failed for %s", key, exc_info=True)
        if entry:
            try:
                current_mtime = os.path.getmtime(entry["source_path"])
            except OSError:
                current_mtime = None
            if (current_mtime is not None and
                    abs(current_mtime - entry["source_mtime"]) < 1e-6):
                try:
                    data = (ART_CACHE_DIR / entry["thumb_file"]).read_bytes()
                    return ("data:image/jpeg;base64," +
                            base64.b64encode(data).decode("ascii"))
                except OSError:
                    log.debug("cached thumbnail missing for %s, "
                              "re-resolving", key)

        jpeg_bytes, source_path, source_mtime = self.resolve(candidate_paths)
        if jpeg_bytes is None:
            return None
        try:
            ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            thumb_file = hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg"
            (ART_CACHE_DIR / thumb_file).write_bytes(jpeg_bytes)
            set_entry(key, source_path, source_mtime, thumb_file)
        except Exception:
            log.warning("could not persist art cache thumbnail for %s",
                        key, exc_info=True)
        return ("data:image/jpeg;base64," +
                base64.b64encode(jpeg_bytes).decode("ascii"))

    @staticmethod
    def _embedded_bytes(audio_path: str) -> bytes | None:
        suffix = Path(audio_path).suffix.lower()
        try:
            if suffix == ".mp3":
                from mutagen.id3 import ID3, APIC

                for frame in ID3(audio_path).values():
                    if isinstance(frame, APIC) and frame.data:
                        return frame.data
            elif suffix == ".flac":
                from mutagen.flac import FLAC

                pictures = FLAC(audio_path).pictures
                if pictures:
                    return pictures[0].data
            else:
                from mutagen import File

                meta = File(audio_path)
                pictures = getattr(meta, "pictures", None)
                if pictures:
                    return pictures[0].data
                # WAV and a few others carry ID3 rather than FLAC-style
                # pictures, so there is nothing on .pictures to find and the
                # art was being missed even though the file has it.
                tags = getattr(meta, "tags", None)
                if tags is not None:
                    # True Vorbis containers (OGG/Opus) have no native
                    # picture block; a cover there rides as a base64
                    # -encoded FLAC Picture block under this key instead,
                    # so .pictures above never sees it.
                    block = tags.get("metadata_block_picture")
                    if block:
                        import base64

                        from mutagen.flac import Picture

                        try:
                            pic = Picture(base64.b64decode(block[0]))
                            if pic.data:
                                return pic.data
                        except Exception:
                            log.debug("could not decode metadata_block_"
                                      "picture in %s", audio_path,
                                      exc_info=True)

                    from mutagen.id3 import APIC

                    for frame in getattr(tags, "values", lambda: [])():
                        if isinstance(frame, APIC) and frame.data:
                            return frame.data
        except Exception:
            log.debug("no embedded art in %s", audio_path, exc_info=True)
        return None

    def clear(self) -> None:
        with self._lock:
            self._urls.clear()


def read_full_source_bytes(candidate_paths):
    """Find the same source resolve() would, but return it untouched.

    resolve() decodes and re-encodes down to the app's own small display
    size, which is correct for the library grid and Now Playing, but
    wrong for anything that needs the true original: copying art to the
    system clipboard, or re-cropping an already-embedded cover without
    quietly starting from a downsized copy of it.

    Returns (raw_bytes, mime, source_path), or (None, None, None) if
    nothing was found in any candidate. mime is best-effort from the
    image's actual format, not assumed from the file extension.
    """
    for path in candidate_paths:
        raw = ArtProvider._embedded_bytes(path)
        source = path
        if raw is None:
            folder = Path(path).parent
            for name in COVER_NAMES:
                candidate = folder / name
                try:
                    if candidate.is_file():
                        raw = candidate.read_bytes()
                        source = str(candidate)
                        break
                except Exception:
                    log.debug("cover file %s could not be read",
                              candidate, exc_info=True)
                    continue
        if raw:
            mime = "image/jpeg"
            try:
                from PIL import Image

                fmt = (Image.open(BytesIO(raw)).format or "JPEG").upper()
                if fmt == "PNG":
                    mime = "image/png"
                elif fmt in ("JPEG", "JPG"):
                    mime = "image/jpeg"
                else:
                    mime = f"image/{fmt.lower()}"
            except Exception:
                log.debug("could not read format of %s", source,
                          exc_info=True)
            return raw, mime, source
    return None, None, None


def prepare_embed_jpeg(image_bytes: bytes, size: int = EMBED_ART_SIZE) -> bytes:
    """Turn arbitrary image bytes into a clean JPEG for embedding.

    Never forced to square - many real covers (tall DVD-style inserts,
    for one) legitimately aren't, and the app's own display already
    handles a non-square cover correctly (see CSS contain-fit), so
    there's no reason to crop or pad one on the way into the file. Only
    the longer side is capped at `size`; the shorter side follows
    whatever the source's own aspect ratio is.
    """
    from PIL import Image

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) != size:
        scale = size / max(w, h)
        new_w = max(1, round(w * scale))
        new_h = max(1, round(h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
