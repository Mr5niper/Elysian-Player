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
from concurrent.futures import ThreadPoolExecutor
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
            "compilation", "dir", "modified_at", "added_at")

_UPSERT = f"""
    INSERT INTO tracks ({','.join(_COLUMNS)})
    VALUES ({','.join('?' * len(_COLUMNS))})
    ON CONFLICT(key) DO UPDATE SET
        path=excluded.path, title=excluded.title, artist=excluded.artist,
        album=excluded.album, album_artist=excluded.album_artist,
        genre=excluded.genre, duration=excluded.duration,
        track_number=excluded.track_number, disc_number=excluded.disc_number,
        year=excluded.year, compilation=excluded.compilation,
        dir=excluded.dir, modified_at=excluded.modified_at
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


def _read_job(job):
    """Read one file's tags. Never raises, so one bad file cannot stop a scan."""
    path, key, mtime = job
    try:
        meta = read_metadata(path)
    except Exception:
        log.exception("library scan recovered from %s", path)
        meta = {}
    return path, key, mtime, meta


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
                        compilation   INTEGER DEFAULT 0,
                        dir           TEXT    DEFAULT '',
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
                # Older databases predate the compilation column. Add it,
                # and clear modified_at so the next scan actually re-reads
                # the files: without that every track looks unchanged and
                # the new column would stay empty forever.
                have = {r["name"] for r in con.execute("PRAGMA table_info(tracks)")}
                if "dir" not in have:
                    con.execute("ALTER TABLE tracks ADD COLUMN dir TEXT DEFAULT ''")
                    con.execute("UPDATE tracks SET modified_at = 0")
                    log.info("library upgraded; a rescan will fill in the "
                             "folder column")
                if "compilation" not in have:
                    con.execute("ALTER TABLE tracks "
                                "ADD COLUMN compilation INTEGER DEFAULT 0")
                    con.execute("UPDATE tracks SET modified_at = 0")
                    log.info("library upgraded; a rescan will fill in the "
                             "compilation flag")
                # Indexes are created after the column checks above, not in
                # the schema script: on an existing table the CREATE TABLE is
                # skipped, so an index naming a newly added column would fail
                # and abort the whole script before the migration ran.
                con.execute("CREATE INDEX IF NOT EXISTS idx_dir ON tracks(dir)")
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

    def _walk_dirs(self, root: str):
        """Yield one folder at a time as (folder, audio files inside it).

        A folder at a time rather than the whole tree first: a large
        collection can take minutes to walk, and nothing should have to
        wait for the end of that before it appears.
        """
        stack = [root]
        while stack:
            if self._cancel.is_set():
                return
            folder = stack.pop()
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
            except OSError:
                continue
            subdirs, files = [], []
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                    elif os.path.splitext(entry.name)[1].lower() \
                            in config.AUDIO_EXTENSIONS:
                        files.append(pathutil.absolute(entry.path))
                except OSError:
                    continue
            stack.extend(reversed(subdirs))
            if files:
                yield pathutil.absolute(folder), files

    def _scan_directory(self, folder, files, pool) -> dict:
        """Index one folder and commit it, so its albums appear right away.

        The database lock is taken only for the reads and writes, never
        across the tag reading: holding it for a whole scan blocked every
        query behind it, which on a large collection meant a library that
        looked frozen for minutes.
        """
        dkey = pathutil.key(folder)
        with self._lock:
            con = self._connect()
            try:
                known = {r["key"]: float(r["modified_at"] or 0.0)
                         for r in con.execute(
                             "SELECT key, modified_at FROM tracks WHERE dir = ?",
                             (dkey,))}
            finally:
                con.close()

        todo, seen = [], set()
        for path in files:
            key = pathutil.key(path)
            seen.add(key)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if key in known and abs(known[key] - mtime) < 1e-4:
                continue
            todo.append((path, key, mtime))

        rows = []
        if todo and not self._cancel.is_set():
            now = time.time()
            for path, key, mtime, meta in pool.map(_read_job, todo):
                if self._cancel.is_set():
                    break
                rows.append((
                    path, key,
                    meta.get("title", "") or Path(path).stem,
                    meta.get("artist", ""), meta.get("album", ""),
                    meta.get("album_artist", "") or meta.get("artist", ""),
                    meta.get("genre", ""),
                    float(meta.get("length", 0.0) or 0.0),
                    int(meta.get("track_number", 0) or 0),
                    int(meta.get("disc_number", 0) or 0),
                    int(meta.get("year", 0) or 0),
                    int(meta.get("compilation", 0) or 0),
                    dkey, float(mtime or 0.0), now,
                ))

        stale = [k for k in known if k not in seen]
        if rows or stale:
            with self._lock:
                con = self._connect()
                try:
                    for i in range(0, len(rows), BATCH_SIZE):
                        con.executemany(_UPSERT, rows[i:i + BATCH_SIZE])
                    if stale:
                        con.executemany("DELETE FROM tracks WHERE key = ?",
                                        [(k,) for k in stale])
                    con.commit()
                except Exception:
                    log.exception("could not write folder %s", folder)
                finally:
                    con.close()
        return {"folder": folder, "scanned": len(files),
                "updated": len(rows), "removed": len(stale), "dir_key": dkey}

    def _prune_missing_dirs(self, root: str, visited: set) -> int:
        """Drop rows for folders under this root that no longer exist.

        Returns the number of tracks dropped, not folders: it is added to
        the removed count the status line reports, and reporting folders
        there made a vanished album look like one lost track.
        """
        with self._lock:
            con = self._connect()
            try:
                have = [r["dir"] for r in con.execute(
                    "SELECT DISTINCT dir FROM tracks WHERE key LIKE ? ESCAPE '\\'",
                    (_like_prefix(root),))]
                gone = [d for d in have if d and d not in visited]
                if not gone:
                    return 0
                dropped = 0
                for d in gone:
                    cur = con.execute("DELETE FROM tracks WHERE dir = ?", (d,))
                    dropped += cur.rowcount or 0
                con.commit()
                return dropped
            except Exception:
                log.exception("could not prune folders under %s", root)
                return 0
            finally:
                con.close()

    def scan_root(self, root, progress=None) -> dict:
        """Index one folder tree, committing each folder as it finishes.

        Nothing is held back until the end: a folder's tracks are queryable
        as soon as that folder is written, so albums appear steadily rather
        than all at once after a long wait.
        """
        folder = pathutil.absolute(root)
        self._cancel.clear()
        totals = {"root": folder, "scanned": 0, "updated": 0, "removed": 0,
                  "folders": 0, "cancelled": False}
        visited = set()

        with ThreadPoolExecutor(max_workers=SCAN_WORKERS,
                                thread_name_prefix="elysian-library") as pool:
            for directory, files in self._walk_dirs(folder):
                if self._cancel.is_set():
                    break
                result = self._scan_directory(directory, files, pool)
                visited.add(result["dir_key"])
                totals["scanned"] += result["scanned"]
                totals["updated"] += result["updated"]
                totals["removed"] += result["removed"]
                totals["folders"] += 1
                if progress:
                    progress(dict(totals), directory)

        if not self._cancel.is_set():
            totals["removed"] += self._prune_missing_dirs(folder, visited)
        totals["cancelled"] = self._cancel.is_set()
        return totals

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
                   COUNT(DISTINCT {_EFFECTIVE_ALBUM}) AS albums,
                   COUNT(DISTINCT NULLIF(genre,'')) AS genres,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
        """)
        data = rows[0] if rows else {"tracks": 0, "artists": 0, "albums": 0,
                                     "genres": 0, "duration": 0.0}
        data["roots"] = self.get_roots()
        return data

    def albums(self) -> list:
        """One row per album, whatever its tracks say about the artist.

        The album is the unit here; the artist view already separates by
        band. Grouping by band as well split a compilation into one card
        per contributing artist whenever the album artist tag was missing,
        which is exactly when it is needed most.

        An album naming more than one band is a compilation, and those sort
        after the single band albums rather than being scattered among them
        under whichever name happened to come first.

        GROUP BY and ORDER BY repeat the expressions rather than using the
        output aliases: album and album_artist are real column names too,
        and SQLite resolves a bare name to the column, which would group by
        the raw tag instead of the fallback chain.
        """
        rows = self._rows(f"""
            SELECT COALESCE(NULLIF(album,''), 'Unknown Album') AS album,
                   (MAX(compilation) = 1 OR COUNT(DISTINCT COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist')) > 1 OR LOWER(MIN(COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist'))) IN ('various artists','various','va'))           AS is_comp,
                   MIN(COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist'))           AS only_artist,
                   MIN(NULLIF(year,0)) AS year,
                   COUNT(*)            AS tracks,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
            GROUP BY COALESCE(NULLIF(album,''), 'Unknown Album')
            ORDER BY (MAX(compilation) = 1 OR COUNT(DISTINCT COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist')) > 1 OR LOWER(MIN(COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist'))) IN ('various artists','various','va')),
                     CASE WHEN (MAX(compilation) = 1 OR COUNT(DISTINCT COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist')) > 1 OR LOWER(MIN(COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist'))) IN ('various artists','various','va')) THEN '' ELSE MIN(COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist')) END COLLATE NOCASE,
                     CASE WHEN MIN(NULLIF(year,0)) IS NULL THEN 1 ELSE 0 END,
                     MIN(NULLIF(year,0)),
                     COALESCE(NULLIF(album,''), 'Unknown Album') COLLATE NOCASE
        """)
        for row in rows:
            comp = bool(row.pop("is_comp", 0))
            only = (row.pop("only_artist", "") or "").strip()
            row["compilation"] = comp
            row["album_artist"] = "Various Artists" if comp else only
        return rows

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
        sql += f" ORDER BY {_TRACK_ORDER} LIMIT 25"
        return [r["path"] for r in self._rows(sql, (album or "Unknown Album",))]

    def album_tracks(self, album: str, album_artist: str = "") -> list:
        sql = f"""
            SELECT path, title, artist, album, album_artist, genre,
                   duration, track_number, disc_number, year
            FROM tracks
            WHERE {_EFFECTIVE_ALBUM} = ?
        """
        # No artist filter: the grid shows one card per album, so opening
        # one has to return the whole album. Filtering by the card's artist
        # would return nothing at all for a compilation, whose card is
        # labelled Various Artists rather than any name in the tags.
        sql += f" ORDER BY {_TRACK_ORDER}"
        return self._rows(sql, (album or "Unknown Album",))

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
