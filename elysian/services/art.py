"""Album art extraction.

Returns art as a base64 data URL for the web frontend. The float-array
texture path that DearPyGui needed is gone along with that interface.
"""
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

from ..config import ART_CACHE_LIMIT, ART_SIZE, COVER_NAMES
from ..logs import get as _get_logger

log = _get_logger("art")


class ArtProvider:
    def __init__(self, size: int = ART_SIZE, limit: int = ART_CACHE_LIMIT):
        self.size = size
        self.limit = limit
        self._urls: OrderedDict[str, str | None] = OrderedDict()

    def data_url(self, audio_path: str) -> str | None:
        """Return embedded art as a base64 data URL for the web frontend."""
        if audio_path in self._urls:
            self._urls.move_to_end(audio_path)
            return self._urls[audio_path]
        url = self._build_url(audio_path)
        self._urls[audio_path] = url
        while len(self._urls) > self.limit:
            self._urls.popitem(last=False)
        return url

    def _build_url(self, audio_path: str) -> str | None:
        import base64
        from io import BytesIO as _BytesIO

        from PIL import Image

        raw = self._embedded_bytes(audio_path)
        img = None
        if raw:
            try:
                img = Image.open(BytesIO(raw))
            except Exception:
                img = None
        if img is None:
            folder = Path(audio_path).parent
            for name in COVER_NAMES:
                candidate = folder / name
                try:
                    if candidate.is_file():
                        img = Image.open(candidate)
                        break
                except Exception:
                    continue
        if img is None:
            return None
        try:
            img = img.convert("RGB")
            img.thumbnail((self.size * 2, self.size * 2), Image.LANCZOS)
            buf = _BytesIO()
            img.save(buf, format="JPEG", quality=86)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            return "data:image/jpeg;base64," + data
        except Exception:
            return None

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

                pictures = getattr(File(audio_path), "pictures", None)
                if pictures:
                    return pictures[0].data
        except Exception:
            log.debug("no embedded art in %s", audio_path, exc_info=True)
        return None

    def clear(self) -> None:
        self._urls.clear()
