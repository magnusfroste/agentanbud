"""
Operations: the logged-in side of the site (/ops).

What is public and what is not. Tenders are public records, and so is how the
service is used in aggregate — /analytics and /system stay open on purpose.
What sits here instead is how the machine is doing, which build is running,
what failed, and the buttons that change data. None of it is secret in the
sense a key is, but none of it is the public's business either, and the
buttons must not depend on a key kept in the browser's localStorage, where
any script on the page can read it.

Login is the existing ADMIN_API_KEY, typed once into a form. The session is an
HMAC-signed cookie (HttpOnly, Secure, SameSite=Strict) whose signing key is
derived from ADMIN_API_KEY, so rotating the key logs everyone out and there is
no second secret to manage. With no key configured, login is refused.

No secret is ever rendered, not even here: the page says whether a key is
configured, never what it is.

State kept in memory (request counters, recent errors, failed logins) assumes
one process. The container runs a single uvicorn worker; with several, each
would count its own share.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import shutil
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

LOG = logging.getLogger(__name__)

COOKIE = "ops_session"
SESSION_SECONDS = 8 * 3600

# Failed logins: this many per client within the window locks that client
# out for the window. In memory; a restart forgets, which is acceptable for
# a key of 64 hex characters.
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_S = 15 * 60

CRON_LOG = os.environ.get("CRON_LOG_PATH", "/var/log/agentanbud.log")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "magnusfroste/agentanbud")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def _session_key(admin_key: str) -> bytes:
    return hmac.new(admin_key.encode(), b"agentanbud-ops-session-v1", hashlib.sha256).digest()


def make_session(admin_key: str, now: Optional[float] = None) -> str:
    exp = int((now or time.time()) + SESSION_SECONDS)
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(_session_key(admin_key), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def session_valid(value: Optional[str], admin_key: str, now: Optional[float] = None) -> bool:
    if not value or not admin_key:
        return False
    try:
        exp_s, nonce, sig = value.split(".")
        exp = int(exp_s)
    except ValueError:
        return False
    expected = hmac.new(_session_key(admin_key), f"{exp_s}.{nonce}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return exp > (now or time.time())


def same_origin(request) -> bool:
    """A cookie-authenticated write must come from this site's own pages.
    SameSite=Strict already stops the cookie riding along on a cross-site
    request; this is the second check, and the one that does not depend on
    the browser."""
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host:
        return False
    return urlparse(origin).netloc.lower() == host.lower()


def client_id(request) -> str:
    """Who is knocking, for the failed-login counter only. Behind the tunnel
    every request comes from the same local address; Cloudflare puts the real
    one in CF-Connecting-IP. Hashed: nothing here needs the address itself."""
    raw = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class LoginGuard:
    def __init__(self) -> None:
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def locked(self, who: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        with self._lock:
            q = self._fails[who]
            while q and q[0] < now - LOGIN_WINDOW_S:
                q.popleft()
            return len(q) >= LOGIN_MAX_FAILS

    def fail(self, who: str, now: Optional[float] = None) -> None:
        with self._lock:
            self._fails[who].append(now or time.time())

    def succeed(self, who: str) -> None:
        with self._lock:
            self._fails.pop(who, None)


# ---------------------------------------------------------------------------
# Request counters and recent errors
# ---------------------------------------------------------------------------
_ASSET_EXT = (".js", ".css", ".png", ".svg", ".ico", ".woff", ".woff2", ".webp", ".jpg", ".jpeg", ".gif")


def request_kind(path: str) -> str:
    if path.startswith("/static") or path.endswith(_ASSET_EXT):
        return "asset"
    if path.startswith("/mcp"):
        return "mcp"
    if path.startswith("/ops") or path.startswith("/api/ops"):
        return "ops"
    if path.startswith("/api"):
        return "api"
    return "page"


class RequestStats:
    """Counts requests in memory; flush() writes the totals to request_hourly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf: dict[tuple, list] = defaultdict(lambda: [0, 0.0])
        self.recent_5xx: deque = deque(maxlen=30)

    def record(self, path: str, status: int, ms: float) -> None:
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        key = (hour, request_kind(path), status // 100)
        with self._lock:
            b = self._buf[key]
            b[0] += 1
            b[1] += ms
        if status >= 500:
            # The path, never the query string: queries are search terms.
            self.recent_5xx.append({
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "path": path, "status": status, "ms": round(ms),
            })

    def flush(self, conn) -> int:
        with self._lock:
            items, self._buf = self._buf, defaultdict(lambda: [0, 0.0])
        if not items:
            return 0
        try:
            conn.executemany(
                "INSERT INTO request_hourly (hour, kind, status, n, ms_total) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(hour, kind, status) DO UPDATE SET n = n + excluded.n, "
                "ms_total = ms_total + excluded.ms_total",
                [(h, k, s, v[0], v[1]) for (h, k, s), v in items.items()],
            )
            conn.commit()
        except Exception:
            # Put the counts back rather than lose them; the next flush retries.
            with self._lock:
                for key, v in items.items():
                    b = self._buf[key]
                    b[0] += v[0]
                    b[1] += v[1]
            LOG.exception("request_hourly flush failed")
            return 0
        return len(items)


class RecentErrors(logging.Handler):
    """The last log records at ERROR and above, for the ops page."""

    def __init__(self, size: int = 40) -> None:
        super().__init__(level=logging.ERROR)
        self.items: deque = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage().splitlines()[0][:300]
            exc = ""
            if record.exc_info and record.exc_info[1] is not None:
                exc = f"{type(record.exc_info[1]).__name__}: {str(record.exc_info[1])[:200]}"
            self.items.append({
                "at": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "logger": record.name, "message": msg, "exception": exc,
            })
        except Exception:
            pass


STATS = RequestStats()
ERRORS = RecentErrors()
LOGINS = LoginGuard()
_installed = False
_install_lock = threading.Lock()


def install(db_path: str, connect) -> None:
    """Attach the error handler and start the flusher, once per process."""
    global _installed
    with _install_lock:
        if _installed:
            return
        _installed = True
    logging.getLogger().addHandler(ERRORS)

    def _loop() -> None:
        while True:
            time.sleep(60)
            try:
                conn = connect(db_path)
                try:
                    STATS.flush(conn)
                finally:
                    conn.close()
            except Exception:
                LOG.exception("ops flusher failed")

    threading.Thread(target=_loop, name="ops-flusher", daemon=True).start()


def record_deploy(conn, build: dict, started: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO deploys (started_at, version, commit_sha, built_at) VALUES (?, ?, ?, ?)",
        (started, build.get("version"), build.get("commit"), build.get("built_at")),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The machine, as far as a container can see it
# ---------------------------------------------------------------------------
_CG = Path("/sys/fs/cgroup")


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _cpu_usage_usec() -> Optional[int]:
    stat = _read(_CG / "cpu.stat")
    if not stat:
        return None
    for line in stat.splitlines():
        k, _, v = line.partition(" ")
        if k == "usage_usec":
            return int(v)
    return None


def machine() -> dict:
    """Container numbers come from its cgroup; load and memory totals from
    /proc are the whole host's, and are labelled as such. Nothing here needs
    the Docker socket, and nothing should: that would give a public app root
    on the host."""
    # Every key present, None when unreadable (outside a container there is
    # no cgroup): the template shows a dash instead of failing.
    out: dict = {"cpus": os.cpu_count(), "container_cpu_pct": None, "container_mem_bytes": None,
                 "container_mem_limit": None, "host_load": None, "host_mem_total": None,
                 "host_mem_available": None}

    u0, t0 = _cpu_usage_usec(), time.monotonic()
    if u0 is not None:
        time.sleep(0.5)
        u1, t1 = _cpu_usage_usec(), time.monotonic()
        if u1 is not None:
            # Percent of the whole machine (all cores = 100).
            out["container_cpu_pct"] = round((u1 - u0) / ((t1 - t0) * 1e6) / (os.cpu_count() or 1) * 100, 1)

    cur = _read(_CG / "memory.current")
    mx = _read(_CG / "memory.max")
    if cur:
        out["container_mem_bytes"] = int(cur)
        out["container_mem_limit"] = None if (mx in (None, "max")) else int(mx)

    la = _read(Path("/proc/loadavg"))
    if la:
        out["host_load"] = [float(x) for x in la.split()[:3]]
    mi = _read(Path("/proc/meminfo"))
    if mi:
        kv = {}
        for line in mi.splitlines():
            k, _, v = line.partition(":")
            kv[k] = int(v.strip().split()[0]) * 1024 if v.strip() else 0
        out["host_mem_total"] = kv.get("MemTotal")
        out["host_mem_available"] = kv.get("MemAvailable")
    return out


def storage(db_path: str, conn) -> dict:
    p = Path(db_path)
    out: dict = {}
    try:
        du = shutil.disk_usage(p.parent)
        out["disk_total"], out["disk_used"], out["disk_free"] = du.total, du.used, du.free
    except OSError:
        pass
    for suffix, key in (("", "db_bytes"), ("-wal", "wal_bytes")):
        try:
            out[key] = (p.parent / (p.name + suffix)).stat().st_size
        except OSError:
            out[key] = 0
    counts = {}
    for t in ("tenders", "knowledge", "posts", "post_events", "usage_log", "sync_log", "request_hourly", "deploys"):
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            counts[t] = None
    out["rows"] = counts
    return out


def requests_by_hour(conn, hours: int = 48) -> list[dict]:
    rows = conn.execute(
        "SELECT hour, kind, status, n, ms_total FROM request_hourly "
        "WHERE hour >= strftime('%Y-%m-%dT%H', 'now', ?) ORDER BY hour",
        (f"-{hours} hours",),
    ).fetchall()
    by_hour: dict[str, dict] = {}
    for r in rows:
        h = by_hour.setdefault(r["hour"], {"hour": r["hour"], "total": 0, "errors": 0, "ms": 0.0,
                                           "page": 0, "api": 0, "mcp": 0, "asset": 0, "ops": 0})
        h["total"] += r["n"]
        h[r["kind"]] = h.get(r["kind"], 0) + r["n"]
        h["ms"] += r["ms_total"]
        if r["status"] == 5:
            h["errors"] += r["n"]
    out = []
    for h in by_hour.values():
        h["avg_ms"] = round(h["ms"] / h["total"]) if h["total"] else 0
        del h["ms"]
        out.append(h)
    return out


def cron_log_tail(lines: int = 40) -> list[str]:
    try:
        with open(CRON_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            text = f.read().decode("utf-8", errors="replace")
        return text.splitlines()[-lines:]
    except OSError:
        return []


def deploys(conn, limit: int = 15) -> list[dict]:
    rows = conn.execute(
        "SELECT started_at, version, commit_sha, built_at FROM deploys ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        sha = d.get("commit_sha") or ""
        d["url"] = f"https://github.com/{GITHUB_REPO}/commit/{sha}" if len(sha) >= 7 else None
        out.append(d)
    return out


def recent_mcp_calls(conn, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT created_at AS ts, action, query, json_extract(meta, '$._sid') AS sid FROM usage_log "
        "WHERE channel = 'mcp' AND action LIKE 'tool:%' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"at": (r["ts"] or "")[:19], "tool": r["action"][5:], "query": r["query"], "session": r["sid"]}
            for r in rows]


def config_status(admin_key: str, allow_open_admin: bool) -> dict:
    """What is configured, never with what. A key is reported as defined or
    not; its value does not leave the environment."""
    # The same toggles docker-compose.yml passes, each defaulting to on.
    scrapers = {name: os.environ.get(f"SCRAPE_{name.upper()}", "true").lower() != "false"
                for name in ("mercell", "ted", "ted_awards", "ted_pin", "lov",
                             "kommersannons", "criteria", "questions")}
    return {
        "admin_key_defined": bool(admin_key),
        "open_admin": allow_open_admin,
        "cron_schedule": os.environ.get("CRON_SCHEDULE", "0 6 * * *"),
        "log_level": os.environ.get("LOG_LEVEL", "INFO"),
        "scrapers": scrapers,
    }
