"""
Write build_info.json — the commit an image was built from.

Runs in the Dockerfile's `meta` stage. Resolves the SHA by reading `.git`
directly rather than shelling out to git, so the builder image needs no extra
package. Falls back to build args, then to "unknown" — it must never fail the
build, because a missing version is an annoyance and a broken build is an
outage.

Usage: python scripts/stamp_build.py <outfile> [fallback_sha] [build_time]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def resolve_sha(repo: Path) -> str | None:
    """Read .git/HEAD and follow the ref to a commit SHA."""
    head_file = repo / ".git" / "HEAD"
    try:
        head = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not head.startswith("ref:"):
        return head or None                      # detached HEAD holds the SHA

    ref = head.split(" ", 1)[1].strip()
    ref_file = repo / ".git" / ref
    try:
        return ref_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass

    # Ref not on disk — a freshly cloned repo keeps it in packed-refs.
    try:
        for line in (repo / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    except OSError:
        pass
    return None


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build_info.json")
    fallback_sha = sys.argv[2] if len(sys.argv) > 2 else ""
    build_time = sys.argv[3] if len(sys.argv) > 3 else ""

    sha = resolve_sha(Path(".")) or fallback_sha or "unknown"
    # This script runs during `docker build`, so "now" is the build time.
    # Defaulting here means Easypanel gets a real timestamp without having to
    # pass a build arg it knows nothing about.
    if not build_time:
        from datetime import datetime, timezone
        build_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    info = {
        "commit": sha,
        "commit_short": sha[:7] if sha != "unknown" else "unknown",
        "built_at": build_time,
    }
    out.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(f"stamped {out}: {info['commit_short']} @ {info['built_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
