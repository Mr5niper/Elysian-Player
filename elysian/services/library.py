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
import re
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
            "compilation", "dir", "track_total", "disc_total",
            "modified_at", "added_at")

_UPSERT = f"""
    INSERT INTO tracks ({','.join(_COLUMNS)})
    VALUES ({','.join('?' * len(_COLUMNS))})
    ON CONFLICT(key) DO UPDATE SET
        path=excluded.path, title=excluded.title, artist=excluded.artist,
        album=excluded.album, album_artist=excluded.album_artist,
        genre=excluded.genre, duration=excluded.duration,
        track_number=excluded.track_number, disc_number=excluded.disc_number,
        year=excluded.year, compilation=excluded.compilation,
        dir=excluded.dir, track_total=excluded.track_total,
        disc_total=excluded.disc_total, modified_at=excluded.modified_at
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


#: What a track counts as belonging to for album grouping. A track that
#: says it is part of a compilation, or is tagged with a conventional
#: "Various Artists" style album artist, is grouped by that regardless of
#: its own artist; anything else groups strictly by its own artist. That
#: second half matters: without it, any two unrelated single-artist albums
#: that happen to share a title - "Greatest Hits" is common - would look
#: like a multi-artist match and get merged into one card with their
#: tracks interleaved.
_GROUP_ARTIST = (
    "CASE WHEN compilation = 1 "
    f"OR LOWER(COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist')) IN ('various artists','various','va') "
    "THEN 'Various Artists' "
    f"ELSE COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist') END"
)

_LEADING_JUNK = re.compile(r"^[^0-9a-z]+", re.IGNORECASE)


def sort_key(name) -> str:
    """How a name should file alphabetically.

    Leading punctuation is ignored, so '"Weird Al" Yankovic' files under W
    rather than ahead of everything, and case is ignored, so it does not
    matter how a tagger capitalised it. If a name is nothing but
    punctuation the original is used, which at least sorts consistently.
    """
    text = (name or "").strip()
    stripped = _LEADING_JUNK.sub("", text)
    return (stripped or text).casefold()


def _escape_like(needle: str) -> str:
    return (needle.replace("\\", "\\\\")
                  .replace("%", "\\%").replace("_", "\\_"))


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
                        track_total   INTEGER DEFAULT 0,
                        disc_total    INTEGER DEFAULT 0,
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
                if "track_total" not in have:
                    con.execute("ALTER TABLE tracks "
                                "ADD COLUMN track_total INTEGER DEFAULT 0")
                    con.execute("ALTER TABLE tracks "
                                "ADD COLUMN disc_total INTEGER DEFAULT 0")
                    con.execute("UPDATE tracks SET modified_at = 0")
                    log.info("library upgraded; a rescan will fill in the "
                             "track and disc totals")
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
                    dkey, int(meta.get("track_total", 0) or 0),
                    int(meta.get("disc_total", 0) or 0),
                    float(mtime or 0.0), now,
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
                   COUNT(DISTINCT {_EFFECTIVE_ALBUM} || char(31) || ({_GROUP_ARTIST})) AS albums,
                   COUNT(DISTINCT NULLIF(genre,'')) AS genres,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
        """)
        data = rows[0] if rows else {"tracks": 0, "artists": 0, "albums": 0,
                                     "genres": 0, "duration": 0.0}
        data["roots"] = self.get_roots()
        return data

    def _match(self, needle, fields):
        """SQL fragment and arguments restricting rows to a search.

        Each view searches its own subject: the albums tab matches album
        and artist names, and songs match titles. Matching titles in the
        album tab meant a search for a song silently changed what the album
        grid meant.
        """
        text = (needle or "").strip()
        if not text:
            return "", []
        like = "%" + _escape_like(text) + "%"
        clause = "(" + " OR ".join(
            f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ")"
        return clause, [like] * len(fields)

    def albums(self, needle="") -> list:
        """One row per album, whatever its tracks say about the artist.

        The album is the unit here; the artist view already separates by
        band. Grouping by band as well split a compilation into one card
        per contributing artist whenever the album artist tag was missing.

        A compilation is grouped by _GROUP_ARTIST rather than by counting
        how many artists share a title: two unrelated single-artist albums
        that happen to be called "Greatest Hits" are not the same release,
        and merging them on title alone put their tracks in one card,
        interleaved.

        Grouping is case insensitive, or a tagger that wrote NEVERMIND on
        one track and Nevermind on the rest would produce two albums.
        Ordering is done in Python rather than SQL so it can ignore leading
        punctuation; see sort_key.
        """
        clause, args = self._match(needle, ("album", "album_artist", "artist"))
        where = f"WHERE {clause}" if clause else ""
        rows = self._rows(f"""
            SELECT MIN({_EFFECTIVE_ALBUM}) AS album,
                   {_GROUP_ARTIST} AS album_artist,
                   MIN(NULLIF(year,0)) AS year,
                   COUNT(*)      AS tracks,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
            {where}
            GROUP BY {_EFFECTIVE_ALBUM} COLLATE NOCASE, {_GROUP_ARTIST} COLLATE NOCASE
        """, tuple(args))
        for row in rows:
            row["compilation"] = row["album_artist"] == "Various Artists"

        # Bands first, alphabetically then by year, with compilations after
        # them. An album with no year sorts to the end of its band's run.
        rows.sort(key=lambda r: (
            1 if r["compilation"] else 0,
            "" if r["compilation"] else sort_key(r["album_artist"]),
            1 if not r["year"] else 0,
            r["year"] or 0,
            sort_key(r["album"]),
        ))
        return rows

    def artists(self, needle="") -> list:
        clause, args = self._match(needle, ("album_artist", "artist"))
        where = f"WHERE {clause}" if clause else ""
        rows = self._rows(f"""
            SELECT MIN(COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist')) AS artist,
                   COUNT(*)  AS tracks,
                   COUNT(DISTINCT COALESCE(NULLIF(album,''), 'Unknown Album')
                                  COLLATE NOCASE) AS albums,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
            {where}
            GROUP BY COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist') COLLATE NOCASE
        """, tuple(args))
        rows.sort(key=lambda r: sort_key(r["artist"]))
        return rows

    def genres(self, needle="") -> list:
        clause, args = self._match(needle, ("genre",))
        where = "AND " + clause if clause else ""
        rows = self._rows(f"""
            SELECT MIN(genre) AS genre, COUNT(*) AS tracks,
                   COALESCE(SUM(duration),0) AS duration
            FROM tracks
            WHERE COALESCE(genre,'') <> '' {where}
            GROUP BY genre COLLATE NOCASE
        """, tuple(args))
        rows.sort(key=lambda r: sort_key(r["genre"]))
        return rows

    def songs(self, needle="", limit=50000) -> list:
        """Individual tracks, grouped by album the way artists and genres are.

        Ordered in SQL as well as in Python: the cap has to take a
        meaningful first slice, and without an ORDER BY the rows it keeps
        are whatever the table happened to yield.

        The limit is a backstop against a pathological collection, not a
        working limit: ten thousand songs build in about 150ms and cost
        nothing to scroll, since offscreen rows are kept out of the layout
        budget. It is reported when reached, so the pane can say the list
        was cut short rather than quietly lying about what is there.
        """
        # Titles only. Matching the album name as well returned every track
        # on an album whose title happened to contain the search, which is
        # what the albums tab is for; here the rows are songs, so the
        # search should be too.
        clause, args = self._match(needle, ("title",))
        where = f"WHERE {clause}" if clause else ""
        rows = self._rows(f"""
            SELECT path, title, artist, album, album_artist, genre,
                   duration, track_number, disc_number, year
            FROM tracks
            {where}
            ORDER BY CASE WHEN COALESCE(album,'') = '' THEN 1 ELSE 0 END,
                     COALESCE(NULLIF(album_artist,''), NULLIF(artist,''), 'Unknown Artist') COLLATE NOCASE,
                     year, album COLLATE NOCASE, {_TRACK_ORDER}
            LIMIT ?
        """, tuple(args) + (int(limit) + 1,))
        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]
        # Re-sorted here so leading punctuation is ignored, which SQL
        # collation cannot do; within an album the SQL order is kept.
        # Tracks with no album tag collect at the very end rather than
        # after each artist's records, where they were scattered down the
        # length of the list. Everything else keeps album order.
        rows.sort(key=lambda r: (
            1 if not (r["album"] or "").strip() else 0,
            sort_key(r["album_artist"] or r["artist"]),
            r["year"] or 0,
            sort_key(r["album"]),
            max(r["disc_number"] or 1, 1),
            r["track_number"] or 0,
            sort_key(r["title"]),
        ))
        for row in rows:
            row["truncated"] = truncated
        return rows

    def album_paths(self, album: str, album_artist: str = "") -> list:
        """Candidate files for an album's cover, best first.

        Ordered by disc and track so the first one tried is the opening
        track, which is the most likely to carry the artwork. Filtered
        the same way album_tracks is, so a cover lookup for one card
        cannot pull an image from a different, same-titled album.
        """
        sql = ("SELECT path FROM tracks WHERE "
               f"{_EFFECTIVE_ALBUM} = ? COLLATE NOCASE")
        args = [album or "Unknown Album"]
        if album_artist:
            sql += f" AND ({_GROUP_ARTIST}) = ? COLLATE NOCASE"
            args.append(album_artist)
        sql += f" ORDER BY {_TRACK_ORDER} LIMIT 25"
        return [r["path"] for r in self._rows(sql, tuple(args))]

    def album_tracks(self, album: str, album_artist: str = "") -> list:
        """Every track on the card identified by (album, album_artist).

        The filter matches _GROUP_ARTIST, not the raw album_artist
        column: a plain equality check would miss a compilation's
        tracks, most of which do not literally say "Various Artists",
        and would pull a different same-titled artist's album into this
        one otherwise. album_artist is optional only for callers that
        genuinely want every track under a title regardless of card;
        every caller in this codebase supplies it, since it always has
        the value albums() produced.
        """
        sql = f"""
            SELECT path, title, artist, album, album_artist, genre,
                   duration, track_number, disc_number, year
            FROM tracks
            WHERE {_EFFECTIVE_ALBUM} = ? COLLATE NOCASE
        """
        args = [album or "Unknown Album"]
        if album_artist:
            sql += f" AND ({_GROUP_ARTIST}) = ? COLLATE NOCASE"
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
            WHERE {_EFFECTIVE_ARTIST} = ? COLLATE NOCASE
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
            WHERE genre = ? COLLATE NOCASE
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

    # ---- the tag editor uses these --------------------------------------

    #: The fields the editor can show or change. One list, so the editor
    #: payload, the mixed-value aggregation, and the writer's idea of "what
    #: is editable" cannot quietly drift apart from each other.
    EDITABLE_FIELDS = (
        "title", "artist", "album", "album_artist", "genre",
        "track_number", "track_total", "disc_number", "disc_total",
        "year", "compilation",
    )

    def tracks_by_paths(self, paths) -> list:
        """Current indexed rows for exact files, in the order asked for.

        Missing from the index entirely (should not happen: every path this
        is called with came from something the library itself rendered) is
        simply absent from the result rather than an error.
        """
        wanted = [p for p in dict.fromkeys(paths) if p]
        if not wanted:
            return []
        by_key = {pathutil.key(p): p for p in wanted}
        found = {}
        with self._lock:
            con = self._connect()
            try:
                keys = list(by_key)
                for i in range(0, len(keys), 400):
                    chunk = keys[i:i + 400]
                    sql = ("SELECT key, path, title, artist, album, "
                           "album_artist, genre, duration, track_number, "
                           "track_total, disc_number, disc_total, year, "
                           "compilation FROM tracks WHERE key IN (%s)"
                           % ",".join("?" * len(chunk)))
                    for row in con.execute(sql, chunk):
                        d = dict(row)
                        d["path"] = by_key[row["key"]]
                        d.pop("key", None)
                        found[d["path"]] = d
            except Exception:
                log.exception("library tracks_by_paths failed")
            finally:
                con.close()
        return [found[p] for p in wanted if p in found]

    def edit_payload(self, paths) -> dict:
        """Merge current tags for one or many tracks into one editable form.

        A field where every track agrees carries that value; a field where
        they differ comes back blank (or 0) with mixed[field] set, so the
        editor can show "multiple values" instead of a wrong shared one.
        """
        rows = self.tracks_by_paths(paths)
        data, mixed = {}, {}
        for field in self.EDITABLE_FIELDS:
            values = {row.get(field) for row in rows}
            if len(values) <= 1:
                data[field] = next(iter(values), "" if field in
                                   ("title", "artist", "album", "album_artist",
                                    "genre") else 0)
            else:
                data[field] = "" if field in (
                    "title", "artist", "album", "album_artist", "genre"
                ) else 0
                mixed[field] = True
        return {
            "count": len(rows),
            "paths": [row["path"] for row in rows],
            "data": data,
            "mixed": mixed,
        }

    def refresh_paths(self, paths) -> int:
        """Re-read exact files from disk and upsert them into the index.

        Used right after a tag write: the file just changed underneath the
        index, and a full rescan is not needed to notice that, only these
        specific paths. Mirrors the row shape _scan_directory builds, so a
        freshly written file and a freshly scanned one look identical to
        every query in this class.
        """
        clean = [p for p in dict.fromkeys(paths) if p]
        if not clean:
            return 0
        now = time.time()
        rows = []
        for path in clean:
            try:
                meta = read_metadata(path)
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            except Exception:
                log.exception("could not re-read %s after editing", path)
                continue
            key = pathutil.key(path)
            dkey = pathutil.key(os.path.dirname(path))
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
                dkey, int(meta.get("track_total", 0) or 0),
                int(meta.get("disc_total", 0) or 0),
                float(mtime or 0.0), now,
            ))
        if not rows:
            return 0
        with self._lock:
            con = self._connect()
            try:
                for i in range(0, len(rows), BATCH_SIZE):
                    con.executemany(_UPSERT, rows[i:i + BATCH_SIZE])
                con.commit()
            except Exception:
                log.exception("could not write refreshed tags to the index")
                return 0
            finally:
                con.close()
        return len(rows)
