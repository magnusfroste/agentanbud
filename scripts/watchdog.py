#!/usr/bin/env python3
"""
Restart the app when it stops answering.

On 24 October 2026 the container stopped answering at about 01:00 UTC and
went on not answering for nine hours, until a deploy replaced it: no
requests served, the 06:00 sync never ran. The process had not exited, so
Docker's `restart: unless-stopped` had nothing to act on — it restarts a
container that dies, not one that hangs.

cron runs this every two minutes inside the container. It asks /api/health
on localhost; after LIMIT failures in a row (about six minutes) it kills
uvicorn with SIGKILL. scripts/start.sh then exits, the container stops, and
the restart policy starts a fresh one. A hung process gets SIGKILL because
a hung process is exactly the one that may not act on SIGTERM.

Each restart is written to /data/watchdog.log (which outlives the
container, and which the ops overview reads) and to the container's log.

Run by cron: python scripts/watchdog.py
"""
from __future__ import annotations

import json
import os
import signal
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = os.environ.get("WATCHDOG_URL", "http://127.0.0.1:8080/api/health")
STATE = Path(os.environ.get("WATCHDOG_STATE", "/tmp/watchdog.fails"))  # fresh per container
LOG = Path(os.environ.get("WATCHDOG_LOG", "/data/watchdog.log"))
LIMIT = 3
TIMEOUT_S = 20


def healthy(url: str = URL, timeout: float = TIMEOUT_S) -> tuple[bool, str]:
    """(True, "") when /api/health answers 200 with ok: true, else (False, why)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read() or b"{}")
            return (True, "") if body.get("ok") else (False, "health answered without ok: true")
    except Exception as exc:  # timeout, refused, 5xx, bad JSON: all mean "not answering"
        return False, f"{type(exc).__name__}: {exc}"[:200]


def uvicorn_pids(proc: Path = Path("/proc")) -> list[int]:
    """Pids of the app server, found by command line (procps is not installed)."""
    out = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        # The program itself, not anything that mentions it: `uvicorn …` or
        # `python …/uvicorn …` as the first two arguments, app.main among
        # the rest. A shell whose command line merely contains the words
        # (`sh -c "… uvicorn app.main …"`) must not be killed.
        args = [a for a in cmd.split(b"\0") if a]
        if any(a.rsplit(b"/", 1)[-1] == b"uvicorn" for a in args[:2]) and \
                any(a.startswith(b"app.main") for a in args):
            out.append(int(entry.name))
    return sorted(out)


def _record(event: dict, log: Path) -> None:
    line = json.dumps(event, ensure_ascii=False)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(f"watchdog: {line}", flush=True)
    try:  # the container's own log, so `docker logs` shows it too
        with open("/proc/1/fd/1", "w") as f:
            f.write(f"watchdog: {line}\n")
    except OSError:
        pass


def run(url: str = URL, state: Path = STATE, log: Path = LOG, proc: Path = Path("/proc"),
        kill=os.kill, check=healthy) -> str:
    """One check. Returns "ok", "fail N/LIMIT" or "restart"."""
    ok, why = check(url)
    if ok:
        if state.exists():
            state.unlink()
        return "ok"
    try:
        fails = int(state.read_text().strip() or 0) + 1
    except (OSError, ValueError):
        fails = 1
    if fails < LIMIT:
        state.write_text(str(fails))
        return f"fail {fails}/{LIMIT}"
    pids = uvicorn_pids(proc)
    _record({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "action": "restart",
             "fails": fails, "reason": why, "pids": pids}, log)
    for pid in pids:
        try:
            kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if state.exists():
        state.unlink()
    return "restart"


if __name__ == "__main__":
    result = run()
    if result != "ok":
        print(f"watchdog: {result}", flush=True)
    sys.exit(0)
