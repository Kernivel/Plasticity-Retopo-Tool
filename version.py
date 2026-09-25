"""The version/build string shown in the N-panel. Bump it on every change.

It shows whether a reload actually picked up new code.
`deployed_version` reads what is on disk, to compare with what is in memory.
"""
import os
import re
import time

ADDON_VERSION = "0.81.0"
BUILD_ID = "2026-09-25-d"

_VERSION_RE = re.compile(
    r'^(ADDON_VERSION|BUILD_ID)\s*=\s*"([^"]*)"', re.MULTILINE)

# The panel redraws on every mouse move during a session, so the file is read
# at most this often.
_DISK_TTL_SECONDS = 2.0
_disk_cache: tuple[float, tuple[str, str] | None] = (0.0, None)


def deployed_version() -> tuple[str, str] | None:
    """(version, build) as this file reads *on disk*, or None if unreadable.

    Differs from the constants above when a deploy landed but Blender still
    runs the previous code.
    Parsed, not imported: importing would return the in-memory module.
    """
    global _disk_cache

    now = time.monotonic()
    stamped_at, cached = _disk_cache
    if cached is not None and (now - stamped_at) < _DISK_TTL_SECONDS:
        return cached

    try:
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as handle:
            found = dict(_VERSION_RE.findall(handle.read()))
        result = (found["ADDON_VERSION"], found["BUILD_ID"])
    except (OSError, KeyError):
        # Nothing to compare against. Never raise inside a panel draw.
        result = None

    _disk_cache = (now, result)
    return result


def running_version() -> tuple[str, str]:
    return (ADDON_VERSION, BUILD_ID)


def stale_load() -> tuple[str, str] | None:
    """(disk_version, disk_build) when what's on disk isn't what's running,
    else None. None too for an unreadable file.
    """
    disk = deployed_version()
    if disk is None or disk == running_version():
        return None
    return disk
