"""Single source of truth for the version/build string shown in the N-panel,
so you can tell at a glance whether a reload actually picked up new code.
Bumped on every change.

`deployed_version` closes the gap the constants alone leave: they say what
Python has *in memory*, which is not the same thing as what is on disk.
"""
import os
import re
import time

ADDON_VERSION = "0.78.0"
BUILD_ID = "2026-09-14-m"

_VERSION_RE = re.compile(
    r'^(ADDON_VERSION|BUILD_ID)\s*=\s*"([^"]*)"', re.MULTILINE)

# The panel redraws on every mouse move during a session, so the file is read
# at most this often.
_DISK_TTL_SECONDS = 2.0
_disk_cache: tuple[float, tuple[str, str] | None] = (0.0, None)


def deployed_version() -> tuple[str, str] | None:
    """(version, build) as this file reads *on disk*, or None if unreadable.

    Different from the constants above exactly when a deploy has landed but the
    running Blender is still executing the previous code. That state otherwise
    looks identical to a feature that simply doesn't work, and no reload can be
    trusted to clear it -- a reload that leaves one module stale is itself a
    failure mode (see the reload invariant in CLAUDE.md), and the stale module
    may well be the one doing the reloading.

    Parsed rather than imported: importing hands back this very module out of
    sys.modules, i.e. the in-memory values again.
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
        # Deployed without sources, unreadable, hand-edited: nothing to compare
        # against, so say so rather than raise inside a panel draw.
        result = None

    _disk_cache = (now, result)
    return result


def running_version() -> tuple[str, str]:
    return (ADDON_VERSION, BUILD_ID)


def stale_load() -> tuple[str, str] | None:
    """(disk_version, disk_build) when what's on disk isn't what's running,
    else None. False for an unreadable file: an unknown is not a mismatch.
    """
    disk = deployed_version()
    if disk is None or disk == running_version():
        return None
    return disk
