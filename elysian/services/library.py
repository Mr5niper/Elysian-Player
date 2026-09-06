"""Persistent library index.

Reading tags off a share is slow, which is why the playlist goes to such
lengths to fetch only what is on screen. Once the tags are in a local
database that problem mostly goes away: an album's worth of metadata comes
back from an indexed query in microseconds, from any part of the library,
whether or not the share is even reachable.

So this is not only a new way to browse. It is the cache that makes the
expensive part of the existing design unnecessary for anything it has
already seen; see `lookup()`, which the playlist scan consults first.

Three things here are deliberate rather than incidental:

* Writes are batched. A connection and a commit per track measured about
  180 times slower than executemany over a shared connection, and a
  library scan is exactly the case where that difference is felt.
* Path prefixes are matched with a trailing separator and an ESCAPE
  clause. "C:\\Music" as a plain LIKE prefix also matches "C:\\Music2",
  and `_` is a single-character wildcard in LIKE, so "C:\\My_Music" also
  matches "C:\\MyXMusic". Since a scan deletes rows under its root that
  it did not see, getting this wrong deletes another root's tracks.
* Identity goes through paths.key(), the same comparison the playlist
  uses, so the same file reached by a different spelling is one row.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

from .. import config
from .. import paths as pathutil
from ..logs import get as _get_logger
from .scanner import read_metadata

log = _get_logger("library")

#: Rows per executemany. Large enough that the per-commit cost disappears,
#: small enough that a scan interrupted partway has still saved most of
#: its work.
BATCH_SIZE = 400

#: Concurrent tag reads during a scan. These threads spend nearly all
#: their time blocked on a network round trip, which releases the GIL.
SCAN_WORKERS = 8

_COLUMNS = ("path", "key", "title", "artist", "album", "album_artist",
            "genre", "duration", "track_number", "disc_number", "year",
            "modified_at", "added_at")

_UPSERT = f"""
    INSERT INTO tracks ({','.join(_COLUMNS)})
    VALUES ({','.join('?' * len(_COLUMNS))})
    ON CONFLICT(key) DO UPDATE SET
        path=excluded.path, title=excluded.title, artist=excluded.artist,
        album=excluded.album, album_artist=excluded.album_artist,
        genre=excluded.genre, duration=excluded.duration,
        track_number=excluded.track_number, disc_number=excluded.disc_number,
        year=excluded.year, modified_at=excluded.modified_at
"""

#: Falls back through album artist, then track artist, then a placeholder,
#: so a track missing the album-artist tag still groups with its album.
_EFFECTIVE_ARTIST = ("COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), "
                     "'Unknown Artist')")
_EFFECTIVE_ALBUM = "COALESCE(NULLIF(album,''), 'Unknown Album')"

#: Ordering within an album. A missing disc tag means the first disc, not
#: disc zero: a rip where only some files carry TPOS would otherwise put
#: every untagged track ahead of every tagged one, which reads as the file
#: format deciding the order when it is really the tag. A missing track
#: number has no such natural value, so those sort to the end by title.
_TRACK_ORDER = ("CASE WHEN disc_number <= 0 THEN 1 ELSE disc_number END, "
                "CASE WHEN track_number <= 0 THEN 1 ELSE 0 END, "
                "track_number, title COLLATE NOCASE")


def _like_prefix(root: str) -> str:
    """A LIKE pattern matching only paths inside this folder."""
    stem = pathutil.key(root).rstrip("\\/") + os.sep
    for ch in ("\\", "%", "_"):
        stem = stem.replace(ch, "\\" + ch)
    return stem + "%"


class LibraryService:
    def __init__(self, db_path=None):
        self._path = str(db_path or config.LIBRARY_DB_FILE)
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._init_schema()

    # ---- connection ----------------------------------------------------

    def _connect(self):
        con = sqlite3.connect(self._path, timeout=15.0)
        con.row_factory = sqlite3.Row
        # Concurrent readers while a scan writes, and a scan that is not
        # paying a disk sync per batch.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _init_schema(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS roots (
                        key  TEXT PRIMARY KEY,
                        path TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS tracks (
                        id            INTEGER PRIMARY KEY,
                        key           TEXT NOT NULL UNIQUE,
                        path          TEXT NOT NULL,
                        title         TEXT    DEFAULT '',
                        artist        TEXT    DEFAULT '',
                        album         TEXT    DEFAULT '',
                        album_artist  TEXT    DEFAULT '',
                        genre         TEXT    DEFAULT '',
                        duration      REAL    DEFAULT 0,
                        track_number  INTEGER DEFAULT 0,
                        disc_number   INTEGER DEFAULT 0,
                        year          INTEGER DEFAULT 0,
                        modified_at   REAL    DEFAULT 0,
                        added_at      REAL    DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist);
                    CREATE INDEX IF NOT EXISTS idx_album  ON tracks(album);
                    CREATE INDEX IF NOT EXISTS idx_genre  ON tracks(genre);
                    CREATE INDEX IF NOT EXISTS idx_album_group
                        ON tracks(album_artist, album, disc_number, track_number);
                    CREATE INDEX IF NOT EXISTS idx_key ON tracks(key);
                """)
                con.commit()
            except Exception:
                log.exception("could not open the library database")
            finally:
                con.close()

    # ---- roots ---------------------------------------------------------

    def get_roots(self) -> list:
        with self._lock:
            con = self._connect()
            try:
                return [r["path"] for r in con.execute(
                    "SELECT path FROM roots ORDER BY path COLLATE NOCASE")]
            finally:
                con.close()

    def add_root(self, path) -> bool:
        folder = pathutil.absolute(path)
        with self._lock:
            con = self._connect()
            try:
                con.execute("INSERT OR REPLACE INTO roots(key, path) VALUES (?,?)",
                            (pathutil.key(folder), folder))
                con.commit()
                return True
            except Exception:
                log.exception("could not add library root %s", folder)
                return False
            finally:
                con.close()

    def remove_root(self, path) -> int:
        """Forget a root and every track underneath it."""
        folder = pathutil.absolute(path)
        with self._lock:
            con = self._connect()
            try:
                con.execute("DELETE FROM roots WHERE key = ?",
                            (pathutil.key(folder),))
                cur = con.execute(
                    "DELETE FROM tracks WHERE key LIKE ? ESCAPE '\\'",
                    (_like_prefix(folder),))
                con.commit()
                return cur.rowcount or 0
            except Exception:
                log.exception("could not remove library root %s", folder)
                return 0
            finally:
                con.close()

    # ---- scanning ------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    def _walk(self, root: str):
        """Audio files under a folder, breadth-limited only by the tree."""
        stack = [root]
        while stack:
            if self._cancel.is_set():
                return
            folder = stack.pop()
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
            except OSError:
                continue
            subdirs = []
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                    elif os.path.splitext(entry.name)[1].lower() \
                            in config.AUDIO_EXTENSIONS:
                        yield pathutil.absolute(entry.path)
                except OSError:
                    continue
            stack.extend(reversed(subdirs))

    def _known(self, con, root: str) -> dict:
        rows = con.execute(
            "SELECT key, modified_at FROM tracks WHERE key LIKE ? ESCAPE '\\'",
            (_like_prefix(root),))
        return {r["key"]: float(r["modified_at"] or 0.0) for r in rows}

    def scan_root(self, root, progress=None) -> dict:
        """Index one folder. Safe to call from a background thread."""
        folder = pathutil.absolute(root)
        self._cancel.clear()
        scanned = updated = removed = 0

        with self._lock:
            con = self._connect()
            try:
                known = self._known(con, folder)
            finally:
                con.close()

        # Decide what actually needs reading before opening any tags: an
        # unchanged file costs one stat instead of a full tag read, which
        # is what makes a rescan cheap.
        todo, seen = [], set()
        for path in self._walk(folder):
            key = pathutil.key(path)
            seen.add(key)
            scanned += 1
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if key in known and abs(known[key] - mtime) < 1e-4:
                continue
            todo.append((path, key, mtime))

        if todo and not self._cancel.is_set():
            updated = self._read_and_store(todo, progress)

        stale = [k for k in known if k not in seen]
        if stale and not self._cancel.is_set():
            with self._lock:
                con = self._connect()
                try:
                    con.executemany("DELETE FROM tracks WHERE key = ?",
                                    [(k,) for k in stale])
                    con.commit()
                    removed = len(stale)
                finally:
                    con.close()

        return {"root": folder, "scanned": scanned,
                "updated": updated, "removed": removed,
                "cancelled": self._cancel.is_set()}

    def _read_and_store(self, todo, progress=None) -> int:
        """Read tags on several threads, write them in batches."""
        import queue as _queue

        jobs = _queue.Queue()
        for item in todo:
            jobs.put(item)
        results = _queue.Queue()

        def worker():
            while not self._cancel.is_set():
                try:
                    path, key, mtime = jobs.get_nowait()
                except _queue.Empty:
                    return
                try:
                    meta = read_metadata(path)
                except Exception:
                    log.exception("library scan recovered from %s", path)
                    meta = {}
                results.put((path, key, mtime, meta))

        threads = [threading.Thread(target=worker, name=f"elysian-library-{i}",
                                    daemon=True)
                   for i in range(min(SCAN_WORKERS, max(1, len(todo))))]
        for t in threads:
            t.start()

        written = 0
        batch = []
        now = time.time()
        with self._lock:
            con = self._connect()
            try:
                pending = len(todo)
                while pending > 0:
                    try:
                        path, key, mtime, meta = results.get(timeout=0.5)
                    except _queue.Empty:
                        if not any(t.is_alive() for t in threads):
                            break
                        continue
                    pending -= 1
                    batch.append((
                        path, key,
                        meta.get("title", "") or Path(path).stem,
                        meta.get("artist", ""), meta.get("album", ""),
                        meta.get("album_artist", "") or meta.get("artist", ""),
                        meta.get("genre", ""),
                        float(meta.get("length", 0.0) or 0.0),
                        int(meta.get("track_number", 0) or 0),
                        int(meta.get("disc_number", 0) or 0),
                        int(meta.get("year", 0) or 0),
                        float(mtime or 0.0), now,
                    ))
                    if len(batch) >= BATCH_SIZE:
                        con.executemany(_UPSERT, batch)
                        con.commit()
                        written += len(batch)
                        batch = []
                        if progress:
                            progress(written, len(todo))
                if batch:
                    con.executemany(_UPSERT, batch)
                    con.commit()
                    written += len(batch)
                    if progress:
                        progress(written, len(todo))
            except Exception:
                log.exception("could not write library batch")
            finally:
                con.close()
        for t in threads:
            t.join(timeout=0.5)
        return written

    def rescan_all(self, progress=None) -> dict:
        total = {"roots": 0, "scanned": 0, "updated": 0, "removed": 0,
                 "cancelled": False}
        for root in self.get_roots():
            result = self.scan_root(root, progress)
            total["roots"] += 1
            for field in ("scanned", "updated", "removed"):
                total[field] += result[field]
            if result["cancelled"]:
                total["cancelled"] = True
                break
        return total

    # ---- queries -------------------------------------------------------

    def _rows(self, sql, args=()) -> list:
        with self._lock:
            con = self._connect()
            try:
                return [dict(r) for r in con.execute(sql, args)]
            except Exception:
                log.exception("library query failed")
                return []
            finally:
                con.close()

    def summary(self) -> dict:
        rows = self._rows(f"""
            SELECT COUNT(*) AS tracks,
                   COUNT(DISTINCT {_EFFECTIVE_ARTIST}) AS artists,
                   COUNT(DISTINCT {_EFFECTIVE_ARTIST} || '\\u0000' || {_EFFECTIVE_ALBUM})
                       AS albums,
                   COUNT(DISTINCT NULLIF(genre,'')) AS genres,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
        """)
        data = rows[0] if rows else {"tracks": 0, "artists": 0, "albums": 0,
                                     "genres": 0, "duration": 0.0}
        data["roots"] = self.get_roots()
        return data

    def albums(self) -> list:
        # Grouped by band, then chronological within each band, with albums
        # missing a year tag at the end of that band's run rather than the
        # front: a NULL year sorts first in SQLite, which put an undated
        # album ahead of everything the artist actually released.
        #
        # GROUP BY and ORDER BY repeat the expression rather than using the
        # output alias: album_artist, album and artist are all real column
        # names too, and SQLite resolves the bare name to the column, which
        # silently groups by the raw tag instead of the fallback chain.
        return self._rows(f"""
            SELECT {_EFFECTIVE_ARTIST} AS album_artist,
                   {_EFFECTIVE_ALBUM}  AS album,
                   MIN(NULLIF(year,0)) AS year,
                   COUNT(*)            AS tracks,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
            GROUP BY COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist'), COALESCE(NULLIF(album,''), 'Unknown Album')
            ORDER BY COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist') COLLATE NOCASE,
                     CASE WHEN MIN(NULLIF(year,0)) IS NULL THEN 1 ELSE 0 END,
                     year,
                     COALESCE(NULLIF(album,''), 'Unknown Album') COLLATE NOCASE
        """)

    def artists(self) -> list:
        return self._rows(f"""
            SELECT {_EFFECTIVE_ARTIST} AS artist,
                   COUNT(*) AS tracks,
                   COUNT(DISTINCT {_EFFECTIVE_ALBUM}) AS albums,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
            GROUP BY COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist')
            ORDER BY COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist') COLLATE NOCASE
        """)

    def genres(self) -> list:
        return self._rows("""
            SELECT genre, COUNT(*) AS tracks,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
            WHERE COALESCE(genre,'') <> ''
            GROUP BY genre
            ORDER BY genre COLLATE NOCASE
        """)

    def album_paths(self, album: str, album_artist: str = "") -> list:
        """Candidate files for an album's cover, best first.

        Ordered by disc and track so the first one tried is the opening
        track, which is the most likely to carry the artwork.
        """
        sql = f"SELECT path FROM tracks WHERE {_EFFECTIVE_ALBUM} = ?"
        args = [album or "Unknown Album"]
        if album_artist:
            sql += f" AND {_EFFECTIVE_ARTIST} = ?"
            args.append(album_artist)
        sql += f" ORDER BY {_TRACK_ORDER} LIMIT 25"
        return [r["path"] for r in self._rows(sql, tuple(args))]

    def album_tracks(self, album: str, album_artist: str = "") -> list:
        sql = f"""
            SELECT path, title, artist, album, album_artist, genre,
                   duration, track_number, disc_number, year
            FROM tracks
            WHERE {_EFFECTIVE_ALBUM} = ?
        """
        args = [album or "Unknown Album"]
        if album_artist:
            sql += f" AND {_EFFECTIVE_ARTIST} = ?"
            args.append(album_artist)
        sql += f" ORDER BY {_TRACK_ORDER}"
        return self._rows(sql, tuple(args))

    def artist_tracks(self, artist: str) -> list:
        # Tracks with no album tag sort last rather than first. Their year
        # is usually 0 too, so plain "ORDER BY year, album" would put the
        # loose files above the actual records.
        return self._rows(f"""
            SELECT path, title, artist, album, album_artist, genre,
                   duration, track_number, disc_number, year
            FROM tracks
            WHERE {_EFFECTIVE_ARTIST} = ?
            ORDER BY CASE WHEN COALESCE(album,'') = '' THEN 1 ELSE 0 END,
                     year, album COLLATE NOCASE, {_TRACK_ORDER}
        """, (artist,))

    def genre_tracks(self, genre: str) -> list:
        # Grouped by artist then album, with untagged files last within each
        # artist, so the view reads as records rather than a loose pile.
        return self._rows(f"""
            SELECT path, title, artist, album, album_artist, genre,
                   duration, track_number, disc_number, year
            FROM tracks
            WHERE genre = ?
            ORDER BY {_EFFECTIVE_ARTIST} COLLATE NOCASE,
                     CASE WHEN COALESCE(album,'') = '' THEN 1 ELSE 0 END,
                     year, album COLLATE NOCASE, {_TRACK_ORDER}
        """, (genre,))

    def search(self, needle: str, limit: int = 500) -> list:
        like = "%" + needle.replace("\\", "\\\\").replace("%", "\\%") \
                          .replace("_", "\\_") + "%"
        return self._rows(f"""
            SELECT path, title, artist, album, album_artist, genre,
                   duration, track_number, disc_number, year
            FROM tracks
            WHERE title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\'
               OR album LIKE ? ESCAPE '\\' OR album_artist LIKE ? ESCAPE '\\'
            ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE,
                     {_TRACK_ORDER}
            LIMIT ?
        """, (like, like, like, like, int(limit)))

    # ---- the part the playlist uses -------------------------------------

    def lookup_one(self, path):
        """Tags for one file, or None if the library has not seen it.

        Called from the playlist scanner's own threads, so it must stay
        cheap and must never raise.
        """
        try:
            found = self.lookup([path])
        except Exception:
            return None
        return found.get(path)

    def lookup(self, paths) -> dict:
        """Tags for files already indexed, keyed by the path asked for.

        This is why the library pays for itself beyond browsing: a track the
        library has seen needs no tag read at all, however slow the share it
        lives on, and however far it is from what is on screen.
        """
        wanted = {pathutil.key(p): p for p in paths if p}
        if not wanted:
            return {}
        found = {}
        keys = list(wanted)
        with self._lock:
            con = self._connect()
            try:
                for i in range(0, len(keys), 400):
                    chunk = keys[i:i + 400]
                    sql = ("SELECT key, title, artist, album, album_artist, "
                           "genre, duration, track_number, disc_number, year "
                           "FROM tracks WHERE key IN (%s)"
                           % ",".join("?" * len(chunk)))
                    for row in con.execute(sql, chunk):
                        found[wanted[row["key"]]] = {
                            "title": row["title"] or "",
                            "artist": row["artist"] or "",
                            "album": row["album"] or "",
                            "album_artist": row["album_artist"] or "",
                            "genre": row["genre"] or "",
                            "length": float(row["duration"] or 0.0),
                            "track_number": int(row["track_number"] or 0),
                            "disc_number": int(row["disc_number"] or 0),
                            "year": int(row["year"] or 0),
                        }
            except Exception:
                log.exception("library lookup failed")
            finally:
                con.close()
        return found
