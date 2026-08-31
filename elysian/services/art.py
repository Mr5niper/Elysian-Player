"""Album art extraction.

Returns textures as array('f') of RGBA floats, which is what DearPyGui's
add_static_texture wants. The v1 cache grew without bound; this one is an LRU
capped at ART_CACHE_LIMIT entries.
"""
from array import array
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

from ..config import ART_CACHE_LIMIT, ART_SIZE, COVER_NAMES

Texture = tuple[int, int, array]


def _to_texture(img, size: int) -> Texture:
    from PIL import Image

    img = img.convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
    data = array("f", [b / 255.0 for b in canvas.tobytes()])
    return size, size, data


class ArtProvider:
    def __init__(self, size: int = ART_SIZE, limit: int = ART_CACHE_LIMIT):
        self.size = size
        self.limit = limit
        self._cache: OrderedDict[str, Texture | None] = OrderedDict()
        self._urls: OrderedDict[str, str | None] = OrderedDict()

    def get(self, audio_path: str) -> Texture | None:
        if audio_path in self._cache:
            self._cache.move_to_end(audio_path)
            return self._cache[audio_path]
        texture = self._extract(audio_path)
        self._cache[audio_path] = texture
        while len(self._cache) > self.limit:
            self._cache.popitem(last=False)
        return texture

    def _extract(self, audio_path: str) -> Texture | None:
        from PIL import Image

        raw = self._embedded_bytes(audio_path)
        if raw:
            try:
                return _to_texture(Image.open(BytesIO(raw)), self.size)
            except Exception:
                pass
        folder = Path(audio_path).parent
        for name in COVER_NAMES:
            candidate = folder / name
            try:
                if candidate.is_file():
                    return _to_texture(Image.open(candidate), self.size)
            except Exception:
                continue
        return None

    @staticmethod
    def _embedded_bytes(audio_path: str) -> bytes | None:
        suffix = Path(audio_path).suffix.lower()
        try:
            if suffix == ".mp3":
                from mutagen.id3 import ID3, APIC

                tags = ID3(audio_path)
                for frame in tags.values():
                    if isinstance(frame, APIC) and frame.data:
                        return frame.data
            elif suffix == ".flac":
                from mutagen.flac import FLAC

                flac = FLAC(audio_path)
                if flac.pictures:
                    return flac.pictures[0].data
            else:
                from mutagen import File

                meta = File(audio_path)
                pictures = getattr(meta, "pictures", None)
                if pictures:
                    return pictures[0].data
        except Exception:
            return None
        return None

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

    def clear(self) -> None:
        self._cache.clear()
        self._urls.clear()
