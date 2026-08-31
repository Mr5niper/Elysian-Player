"""Path handling.

Two rules the rest of the package follows.

**Comparison goes through `key()`.** Deciding whether two strings name the same
file is Windows-specific -- case-insensitive, backslash-normalised -- and
`pathlib` has no equivalent of `os.path.normcase`. That logic was previously
copied into six places, which is exactly the sort of thing that drifts apart.

**`abspath`, never `resolve()`.** They look interchangeable but are not:
`Path.resolve()` touches the filesystem to follow symlinks, so on a network
share every comparison would become a round trip. `os.path.abspath` is pure
string manipulation.

Elsewhere the package uses `pathlib` for construction and inspection, with two
deliberate exceptions kept on `os.path` for speed. Both are measured in
comments at their call sites.
"""
import os


def key(path) -> str:
    """Canonical form for comparing two paths for identity.

    Case-folded and absolute on Windows, absolute elsewhere. Not for display,
    and not a guarantee the file exists -- no filesystem access happens here.
    """
    return os.path.normcase(os.path.abspath(str(path)))


def same(a, b) -> bool:
    """True if both name the same file."""
    return key(a) == key(b)


def absolute(path) -> str:
    """Absolute form suitable for storing and displaying.

    Unlike `key()` this preserves case, so it is what goes into the playlist.
    Still `abspath` rather than `resolve()`, for the same no-I/O reason.
    """
    return os.path.abspath(str(path))


def keys(paths) -> set:
    """Comparison keys for a collection, for membership tests."""
    return {key(p) for p in paths}
