"""
Which build is this?

Before this existed, the only version anywhere was a hardcoded "0.2.0" that
went 52 commits stale, and the only way to tell which build was live was to
count the tools `/mcp` returned. That made "did the deploy land?" a guess,
and made bug reports impossible to tie to code.

The image is stamped at build time (see the `meta` stage in the Dockerfile).
Outside a container — local dev, tests — we read .git directly so the number
is still real rather than "unknown".
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STAMP = _ROOT / "build_info.json"

# Product version. Bump deliberately; the commit is what identifies a build.
VERSION = "0.3.0"

UNKNOWN = "unknown"


def _from_stamp() -> dict | None:
    try:
        data = json.loads(_STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("commit") or data["commit"] == UNKNOWN:
        return None
    return data


def _from_git() -> dict | None:
    """Local dev fallback — same resolution the build stamp uses."""
    try:
        from scripts.stamp_build import resolve_sha  # noqa: PLC0415
        sha = resolve_sha(_ROOT)
    except Exception:
        sha = None
    if not sha:
        return None
    return {"commit": sha, "commit_short": sha[:7], "built_at": "dev"}


def _resolve() -> dict:
    info = _from_stamp() or _from_git() or {
        "commit": UNKNOWN, "commit_short": UNKNOWN, "built_at": UNKNOWN,
    }
    return {"version": VERSION, **info}


BUILD = _resolve()


def build_info() -> dict:
    """Version payload for /api/health, /mcp and the footer."""
    return dict(BUILD)


def version_string() -> str:
    """e.g. '0.3.0+0e8d9bf' — one token that identifies a deployed build."""
    short = BUILD.get("commit_short", UNKNOWN)
    return VERSION if short == UNKNOWN else f"{VERSION}+{short}"


def started_at() -> str:
    return _STARTED


_STARTED = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
