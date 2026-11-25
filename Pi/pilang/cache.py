"""Per-user build cache for the Pi toolchain.

PR.md 1.2 / 2.6 / N2: Pi used to build the C++ runtime and worker executables in
predictable ``<tmp>/...`` paths and trust anything that was already there.  On a
shared machine another local user could pre-create those paths and get code
executed as the victim; concurrent builds could also install a half-written
binary.  This module owns the replacement:

* a private per-user cache directory (``0700``; ``$XDG_CACHE_HOME/pi``,
  ``~/.cache/pi`` or ``%LOCALAPPDATA%\\pi``), refused if it is not a real
  directory owned by the current user;
* atomic installs (write a ``mkstemp`` file in the same directory, then
  ``os.replace``) so a reader never observes a partial build;
* ``PI_CACHE_DIR`` for tests/CI that need an isolated cache.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile

# Bump when the build layout/flags change so old cached binaries are not reused.
CACHE_LAYOUT_VERSION = 1

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


class CacheError(RuntimeError):
    """The cache directory is not safe to use."""


def _check_private_dir(path: str) -> None:
    """Refuse symlinks/reparse points and directories owned by someone else."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise CacheError(f"cannot stat Pi cache directory {path}: {e}") from e
    if os.name != "nt":
        if stat.S_ISLNK(st.st_mode):
            raise CacheError(f"refusing to use Pi cache directory {path}: it is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise CacheError(f"refusing to use Pi cache directory {path}: not a directory")
        if st.st_uid != os.getuid():
            raise CacheError(
                f"refusing to use Pi cache directory {path}: it is owned by uid {st.st_uid}, "
                f"not the current user (uid {os.getuid()})")
        if stat.S_IMODE(st.st_mode) & 0o077:
            # We own it: tighten it rather than silently keep a world-writable cache.
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
    else:
        if not stat.S_ISDIR(st.st_mode):
            raise CacheError(f"refusing to use Pi cache directory {path}: not a directory")
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & _REPARSE_POINT:
            raise CacheError(f"refusing to use Pi cache directory {path}: it is a reparse point")
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def cache_root() -> str:
    """Return the private per-user Pi cache directory, creating it if needed."""
    override = os.environ.get("PI_CACHE_DIR")
    if override:
        root = os.path.abspath(os.path.expanduser(override))
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        root = os.path.join(base, "pi")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
        root = os.path.join(base, "pi")
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError as e:
        raise CacheError(
            f"cannot create the Pi cache directory {root}: {e}; "
            "set PI_CACHE_DIR to a writable directory") from e
    _check_private_dir(root)
    return root


def cache_subdir(*parts: str) -> str:
    """A private subdirectory of the cache root (e.g. ``workers``, ``runtime``)."""
    path = os.path.join(cache_root(), *parts)
    os.makedirs(path, mode=0o700, exist_ok=True)
    _check_private_dir(path)
    return path


def is_regular_file(path: str) -> bool:
    """True if *path* is an existing regular file (not a symlink/reparse point)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if os.name == "nt" and getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    return True


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    """Write *data* to *path* atomically (temp file + ``os.replace``)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_temp_output(directory: str, *, stem: str = "out") -> str:
    """Reserve a unique output path in *directory* for an external compiler.

    Returns the path; the caller owns it and must remove or rename it.  Using
    ``mkstemp`` instead of ``path + ".tmp"`` means two concurrent builds of the
    same artifact never share an output file (PR.md P8).
    """
    os.makedirs(directory, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    fd, path = tempfile.mkstemp(prefix=f".{stem}-", suffix=f".tmp{suffix}", dir=directory)
    os.close(fd)
    return path


def install_temp_output(tmp: str, final: str, *, mode: int = 0o700) -> None:
    """Atomically move a compiler's output over the final cache path."""
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, final)


def frontend_limited() -> bool:  # pragma: no cover - diagnostic helper
    """Whether the process is running with the raised frontend limits."""
    return sys.getrecursionlimit() >= 20000
