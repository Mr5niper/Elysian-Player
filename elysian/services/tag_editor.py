"""Writes edited tags back to audio files.

Reading tags is scanner.py's job and indexing them is library.py's; this is
the only place in the app that rewrites a file the user did not put there
themselves. Keeping it separate means a mistake here can only ever touch a
file someone deliberately chose to edit, never a routine scan or a normal
play.

Track and disc numbers are stored on disk as one string, "3" or "3/12", not
as two separate values. The editor exposes the number and the total as two
independent fields, so writing one while leaving the other alone means
reading whatever this specific file currently has for the untouched half
before combining and writing the pair back. That happens here, per file,
rather than upstream from a database value: a database row can be a moment
out of date with the file it describes, and this is the one place already
holding the file open to ask it directly.
"""
import os

from ..logs import get as _get_logger
from .scanner import _number, _total, EDITABLE_TRACK_FIELDS

log = _get_logger("tag_editor")

#: FLAC and OggVorbis/Opus both expose their tags as a plain dict of string
#: lists (Vorbis comments), so one function covers both. Everything else
#: recognised by this app carries ID3, including WAV, whose tags sit inside
#: a RIFF chunk rather than a bare ID3 stream and so needs its own container
#: type even though the frames themselves are identical to MP3's.
_VORBIS_EXTS = {".flac", ".ogg", ".oga", ".opus"}

_TEXT_FIELDS = ("title", "artist", "album", "album_artist", "genre")

#: What the editor may change. Defined once in scanner.py and shared with
#: LibraryService.EDITABLE_FIELDS, so the two cannot silently disagree
#: about what "editable" means.
FIELDS = EDITABLE_TRACK_FIELDS


def _combine(number, total) -> str:
    """"3" and "12" become "3/12". "3" and nothing stays "3". Nothing at
    all, whichever way, becomes an empty string, which means clear the tag
    entirely rather than write a hollow "0"."""
    number = int(number or 0)
    total = int(total or 0)
    if number <= 0 and total <= 0:
        return ""
    if total > 0:
        return f"{number}/{total}"
    return str(number)


def write_many(paths, changes) -> dict:
    """Apply the same edits to every path. Never raises.

    changes carries only the fields actually being changed; a field absent
    from it leaves that file's tag untouched, the same convention this app
    already uses everywhere else an omitted argument means "no change".
    Blank text or a numeric 0 means clear that tag deliberately, which is
    why "not touched" and "cleared" have to be told apart by whether the
    key is present at all, not by what value it holds.
    """
    ok = failed = 0
    results = []
    for path in paths or []:
        path = str(path)
        try:
            _write_one(path, changes)
        except Exception as exc:
            log.warning("could not write tags to %s", path, exc_info=True)
            results.append({"path": path, "ok": False, "error": str(exc)})
            failed += 1
        else:
            results.append({"path": path, "ok": True})
            ok += 1
    return {"ok": ok, "failed": failed, "results": results}


def _write_one(path, changes) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in _VORBIS_EXTS:
        _write_vorbis(path, changes)
    elif ext == ".wav":
        _write_wav(path, changes)
    else:
        _write_id3_file(path, changes)


# ---- ID3: mp3 and wav -----------------------------------------------

def _apply_id3(tags, changes) -> None:
    from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TCON, TRCK, TPOS, TDRC, TCMP

    text_frames = {
        "title": ("TIT2", TIT2), "artist": ("TPE1", TPE1),
        "album": ("TALB", TALB), "album_artist": ("TPE2", TPE2),
        "genre": ("TCON", TCON),
    }
    for field, (name, cls) in text_frames.items():
        if field not in changes:
            continue
        value = str(changes[field] or "").strip()
        if value:
            tags.setall(name, [cls(encoding=3, text=[value])])
        else:
            tags.delall(name)

    def _pair(name, num_field, total_field):
        if num_field not in changes and total_field not in changes:
            return
        existing = tags.get(name)
        raw = existing.text[0] if existing and existing.text else ""
        cur_num, cur_total = _number(raw), _total(raw)
        num = changes.get(num_field, cur_num)
        total = changes.get(total_field, cur_total)
        combined = _combine(num, total)
        if combined:
            tags.setall(name, [TRCK(encoding=3, text=[combined])
                              if name == "TRCK" else
                              TPOS(encoding=3, text=[combined])])
        else:
            tags.delall(name)

    _pair("TRCK", "track_number", "track_total")
    _pair("TPOS", "disc_number", "disc_total")

    if "year" in changes:
        value = int(changes["year"] or 0)
        if value > 0:
            tags.setall("TDRC", [TDRC(encoding=3, text=[str(value)])])
        else:
            tags.delall("TDRC")

    if "compilation" in changes:
        tags.setall("TCMP", [TCMP(encoding=3, text=["1" if changes["compilation"]
                                                     else "0"])])


def _write_id3_file(path, changes) -> None:
    from mutagen.mp3 import MP3

    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    _apply_id3(audio.tags, changes)
    audio.save()


def _write_wav(path, changes) -> None:
    from mutagen.wave import WAVE

    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    _apply_id3(audio.tags, changes)
    audio.save()


# ---- Vorbis comments: flac, ogg, opus --------------------------------

def _write_vorbis(path, changes) -> None:
    from mutagen import File

    audio = File(path)
    if audio is None:
        raise ValueError("unrecognised audio file")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    text_keys = {
        "title": "title", "artist": "artist", "album": "album",
        "album_artist": "albumartist", "genre": "genre",
    }
    for field, key in text_keys.items():
        if field not in changes:
            continue
        value = str(changes[field] or "").strip()
        if value:
            tags[key] = [value]
        elif key in tags:
            del tags[key]

    def _pair(key, num_field, total_field):
        if num_field not in changes and total_field not in changes:
            return
        existing = tags.get(key)
        raw = existing[0] if existing else ""
        cur_num, cur_total = _number(raw), _total(raw)
        num = changes.get(num_field, cur_num)
        total = changes.get(total_field, cur_total)
        combined = _combine(num, total)
        if combined:
            tags[key] = [combined]
        elif key in tags:
            del tags[key]

    _pair("tracknumber", "track_number", "track_total")
    _pair("discnumber", "disc_number", "disc_total")

    if "year" in changes:
        value = int(changes["year"] or 0)
        if value > 0:
            tags["date"] = [str(value)]
        elif "date" in tags:
            del tags["date"]

    if "compilation" in changes:
        tags["compilation"] = ["1" if changes["compilation"] else "0"]

    audio.save()


# ---- cover art --------------------------------------------------------

#: FLAC has its own native picture block (clear_pictures/add_picture).
#: True Vorbis containers (OGG/Opus) have no such block; a picture there
#: rides as a base64-encoded METADATA_BLOCK_PICTURE comment instead, so
#: art needs its own split even though both share _VORBIS_EXTS for text.
_FLAC_EXTS = {".flac"}
_OGG_EXTS = {".ogg", ".oga", ".opus"}


def write_art_many(paths, jpeg_bytes) -> dict:
    """Embed the same cover image into every path. Never raises.

    jpeg_bytes is expected to already be a clean, correctly-sized JPEG
    (see art.prepare_embed_jpeg) - this only handles getting those exact
    bytes into whichever container each file uses.
    """
    ok = failed = 0
    results = []
    for path in paths or []:
        path = str(path)
        try:
            _write_art_one(path, jpeg_bytes)
        except Exception as exc:
            log.warning("could not write art to %s", path, exc_info=True)
            results.append({"path": path, "ok": False, "error": str(exc)})
            failed += 1
        else:
            results.append({"path": path, "ok": True})
            ok += 1
    return {"ok": ok, "failed": failed, "results": results}


def _write_art_one(path, jpeg_bytes) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in _FLAC_EXTS:
        _write_art_flac(path, jpeg_bytes)
    elif ext in _OGG_EXTS:
        _write_art_ogg(path, jpeg_bytes)
    else:
        _write_art_id3(path, jpeg_bytes)


def _write_art_id3(path, jpeg_bytes) -> None:
    """MP3 and WAV both carry ID3 frames, WAV's inside a RIFF chunk."""
    from mutagen.id3 import APIC

    if os.path.splitext(path)[1].lower() == ".wav":
        from mutagen.wave import WAVE

        audio = WAVE(path)
    else:
        from mutagen.mp3 import MP3

        audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    # Cleared first: leaving a stale APIC alongside a new one means some
    # readers show the old cover, others the new one, depending on which
    # frame they pick.
    audio.tags.delall("APIC")
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                        desc="Cover", data=jpeg_bytes))
    audio.save()


def _write_art_flac(path, jpeg_bytes) -> None:
    from mutagen.flac import FLAC, Picture

    audio = FLAC(path)
    pic = Picture()
    pic.data = jpeg_bytes
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    audio.clear_pictures()
    audio.add_picture(pic)
    audio.save()


def _write_art_ogg(path, jpeg_bytes) -> None:
    """OGG/Opus have no native picture block; the standard convention is
    a base64-encoded FLAC Picture block stored as a Vorbis comment."""
    import base64

    from mutagen import File
    from mutagen.flac import Picture

    audio = File(path)
    if audio is None:
        raise ValueError("unrecognised audio file")
    if audio.tags is None:
        audio.add_tags()
    pic = Picture()
    pic.data = jpeg_bytes
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    encoded = base64.b64encode(pic.write()).decode("ascii")
    audio.tags["metadata_block_picture"] = [encoded]
    audio.save()
