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
import json
import hmac
import logging
import os
import secrets
import shutil
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
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


def request_kind(path: str, method: str = "GET", accept: str = "") -> str:
    if path.startswith("/static") or path.endswith(_ASSET_EXT):
        return "asset"
    if path.startswith("/mcp"):
        # A client opening a server-to-client stream: answered 405 since #49.
        # Counted on its own because a client that misreads the answer
        # reconnects in a loop — 118,000 of these in a day once, all filed
        # under "MCP" and indistinguishable from real tool calls.
        if method == "GET" and "text/event-stream" in (accept or "").lower():
            return "mcp_stream"
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

    def record(self, path: str, status: int, ms: float,
               method: str = "GET", accept: str = "") -> None:
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        key = (hour, request_kind(path, method, accept), status // 100)
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
        last_rollup = 0.0
        while True:
            time.sleep(60)
            try:
                conn = connect(db_path)
                try:
                    STATS.flush(conn)
                    # Days for the growth page: at start, then hourly. Off the
                    # request path: it groups all of usage_log.
                    if time.monotonic() - last_rollup > 3600:
                        rollup_daily(conn)
                        last_rollup = time.monotonic()
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


def _empty_hour(hour: str) -> dict:
    return {"hour": hour, "total": 0, "errors": 0, "ms": 0.0, "page": 0, "api": 0,
            "mcp": 0, "mcp_stream": 0, "asset": 0, "ops": 0}


def requests_by_hour(conn, hours: int = 48) -> list[dict]:
    rows = conn.execute(
        "SELECT hour, kind, status, n, ms_total FROM request_hourly "
        "WHERE hour >= strftime('%Y-%m-%dT%H', 'now', ?) ORDER BY hour",
        (f"-{hours} hours",),
    ).fetchall()
    by_hour: dict[str, dict] = {}
    for r in rows:
        h = by_hour.setdefault(r["hour"], _empty_hour(r["hour"]))
        h["total"] += r["n"]
        if r["kind"] == "mcp_stream":
            # Part of MCP, and also shown on its own.
            h["mcp"] += r["n"]
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
        "SELECT created_at AS ts, action, query, json_extract(meta, '$._sid') AS sid, "
        "json_extract(meta, '$.v') AS v FROM usage_log "
        "WHERE channel = 'mcp' AND action LIKE 'tool:%' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    # `visitor` is the useful column: many clients open a new session per call.
    return [{"at": (r["ts"] or "")[:19], "tool": r["action"][5:], "query": r["query"],
             "session": r["sid"], "visitor": r["v"]} for r in rows]


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


# ---------------------------------------------------------------------------
# Login log
# ---------------------------------------------------------------------------
LOGIN_KEEP_DAYS = 90


def record_login(conn, request, outcome: str) -> None:
    """One row per attempt. Rare enough to write directly; a failure to log
    must never be the reason a login fails, so callers wrap this."""
    conn.execute(
        "INSERT INTO ops_logins (outcome, client, country, user_agent) VALUES (?, ?, ?, ?)",
        (outcome, client_id(request)[:12],
         (request.headers.get("cf-ipcountry") or "")[:2].upper() or None,
         (request.headers.get("user-agent") or "")[:160] or None),
    )
    conn.execute("DELETE FROM ops_logins WHERE at < datetime('now', ?)", (f"-{LOGIN_KEEP_DAYS} days",))
    conn.commit()


def logins(conn, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT at, outcome, client, country, user_agent FROM ops_logins ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()]


def login_summary(conn, hours: int = 24) -> dict:
    rows = conn.execute(
        "SELECT outcome, COUNT(*) n, COUNT(DISTINCT client) clients FROM ops_logins "
        "WHERE at >= datetime('now', ?) GROUP BY outcome", (f"-{hours} hours",)).fetchall()
    by = {r["outcome"]: r["n"] for r in rows}
    failed = sum(n for o, n in by.items() if o not in ("inloggad", "utloggad"))
    last_ok = conn.execute(
        "SELECT at, country FROM ops_logins WHERE outcome = 'inloggad' ORDER BY id DESC LIMIT 1").fetchone()
    return {"ok": by.get("inloggad", 0), "failed": failed, "by_outcome": by,
            "last_ok": dict(last_ok) if last_ok else None}


def uptime(started: str) -> str:
    try:
        t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "—"
    s = int((datetime.now(timezone.utc) - t0).total_seconds())
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d} d {h} h" if d else (f"{h} h {m} min" if h else f"{m} min")


def traffic(rows: list[dict]) -> dict:
    """Totals over the hours given (requests_by_hour rows)."""
    tot = sum(r["total"] for r in rows)
    ms = sum(r["avg_ms"] * r["total"] for r in rows)
    return {"total": tot, "errors": sum(r["errors"] for r in rows),
            "page": sum(r["page"] for r in rows), "api": sum(r["api"] for r in rows),
            "mcp": sum(r["mcp"] for r in rows), "avg_ms": round(ms / tot) if tot else 0}


# The pages of the ops central, in the order the side panel shows them.
SECTIONS = [
    ("", "Översikt"),
    ("maskinen", "Maskinen"),
    ("trafik", "Trafik"),
    ("fel", "Fel"),
    ("synk", "Synk"),
    ("tillvaxt", "Tillväxt"),
    ("agenter", "Agenter"),
    ("inloggningar", "Inloggningar"),
    ("versioner", "Versioner"),
    ("konfiguration", "Konfiguration"),
]


def hour_slots(rows: list[dict], hours: int = 48) -> list[dict]:
    """The last `hours` hours, oldest first, empty hours included. The chart
    is scaled to the slots, not to how many hours happen to have data, or one
    hour of data fills the whole width."""
    by = {r["hour"]: r for r in rows}
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for i in range(hours - 1, -1, -1):
        h = (now - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        if h in by:
            out.append(by[h])
        else:
            e = _empty_hour(h)
            del e["ms"]
            e.update(avg_ms=0, mcp_tools=0, mcp_handshakes=0, mcp_other=0)
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# What the MCP requests were, per hour
# ---------------------------------------------------------------------------
def with_mcp_breakdown(conn, rows: list[dict], hours: int = 48) -> list[dict]:
    """Split each hour's MCP requests into tool calls, handshakes, stream
    requests and the rest (tool listings, notifications, plain GETs).

    The request counter only sees paths. Tool calls and handshakes come from
    usage_log, which records both — so this also works for hours counted
    before stream requests had a kind of their own: a storm of empty
    reconnects shows as thousands of MCP requests and almost no tool calls.
    """
    counts = {r["hour"]: r for r in conn.execute(
        "SELECT strftime('%Y-%m-%dT%H', created_at) AS hour, "
        "SUM(action LIKE 'tool:%') AS tools, SUM(action = 'mcp:connect') AS handshakes "
        "FROM usage_log WHERE channel = 'mcp' AND created_at >= datetime('now', ?) GROUP BY hour",
        (f"-{hours} hours",)).fetchall()}
    for h in rows:
        c = counts.get(h["hour"])
        tools = (c["tools"] if c else 0) or 0
        hand = (c["handshakes"] if c else 0) or 0
        h["mcp_tools"], h["mcp_handshakes"] = tools, hand
        h["mcp_other"] = max(0, h.get("mcp", 0) - tools - hand - h.get("mcp_stream", 0))
    return rows


# ---------------------------------------------------------------------------
# Traffic spikes
# ---------------------------------------------------------------------------
SPIKE_FACTOR = 5        # an hour this many times the median hour is a spike
SPIKE_FLOOR = 2000      # ...but never below this, or a quiet night flags a busy morning


def traffic_spikes(slots: list[dict]) -> list[dict]:
    """Hours in the last 24 well above the 48-hour median.

    The warnings used to watch errors, syncs, logins and disk — not volume.
    On 12 October MCP requests ran at ~31,000 an hour for four hours, about
    40 times normal, and the overview said "Allt ser bra ut".
    """
    totals = sorted(s["total"] for s in slots if s["total"])
    if len(totals) < 6:
        return []  # too little history to call anything unusual
    median = totals[len(totals) // 2]
    limit = max(SPIKE_FACTOR * median, SPIKE_FLOOR)
    return [{**s, "factor": round(s["total"] / median) if median else None, "median": median}
            for s in slots[-24:] if s["total"] > limit]


def spike_warning(spikes: list[dict]) -> Optional[str]:
    if not spikes:
        return None
    worst = max(spikes, key=lambda s: s["total"])
    what = ""
    mcp, total = worst.get("mcp", 0), worst["total"] or 1
    if mcp / total > 0.8:
        empty = worst.get("mcp_other", 0) + worst.get("mcp_stream", 0)
        what = (" — nästan bara MCP utan verktygsanrop" if empty / max(mcp, 1) > 0.9
                else " — mest MCP")
    elif worst.get("api", 0) / total > 0.8:
        what = " — mest API"
    hours = f"{len(spikes)} timmar" if len(spikes) > 1 else "1 timme"
    n = f"{worst['total']:,}".replace(",", "\u202f")
    return (f"Ovanligt mycket trafik ({hours} senaste dygnet): {n} anrop kl. "
            f"{worst['hour'][11:13]} UTC, {worst['factor']}× normalt{what}")


# ---------------------------------------------------------------------------
# Who is calling
# ---------------------------------------------------------------------------
# Filters a search can carry instead of (or as well as) a search word, as
# labels for the ops page. API searches log source/authority/cpv/buyer_type;
# MCP tool calls log their arguments, which use the same names.
_FILTERS = (("source", "källa"), ("cpv", "cpv"), ("authority", "köpare"),
            ("buyer_type", "köpartyp"), ("region", "region"))


def describe_call(query: Optional[str], meta: dict) -> str:
    """What one search asked for: the search word, else its filters in
    brackets. Two watchers polled the API every five minutes with no search
    word at all, and the page could only say they asked "nothing"."""
    if query:
        return query
    parts = [f"{label} {meta[k]}" for k, label in _FILTERS if meta.get(k)]
    return "[" + ", ".join(parts) + "]" if parts else "[inga filter]"


def top_callers(conn, hours: int = 24, limit: int = 10) -> dict:
    """Searches and tool calls per visitor id over the last `hours`.

    The recent-calls table showed a loop — the same eight questions every
    five minutes — but not who: every call opened a new MCP session, so the
    session column said nothing. The visitor id (app/visitors.py) is the
    same for one caller all day, so grouping on it shows the loop as one row.
    """
    rows = conn.execute(
        "SELECT json_extract(meta, '$.v') AS v, channel, LOWER(TRIM(query)) AS q, meta, created_at "
        "FROM usage_log WHERE created_at >= datetime('now', ?) "
        "AND json_extract(meta, '$.v') IS NOT NULL AND (action LIKE 'tool:%' OR action = 'search') "
        "ORDER BY created_at", (f"-{hours} hours",)).fetchall()
    clients: dict[str, str] = {}
    for r in conn.execute(
            "SELECT json_extract(meta, '$.v') AS v, json_extract(meta, '$.client') AS client "
            "FROM usage_log WHERE action = 'mcp:connect' AND created_at >= datetime('now', ?) "
            "AND json_extract(meta, '$.v') IS NOT NULL ORDER BY id", (f"-{hours} hours",)):
        if r["client"]:
            clients[r["v"]] = r["client"]
    by: dict[str, dict] = {}
    for r in rows:
        b = by.setdefault(r["v"], {"v": r["v"], "n": 0, "channels": set(), "queries": defaultdict(int),
                                   "times": [], "programs": defaultdict(int)})
        b["n"] += 1
        b["channels"].add(r["channel"])
        try:
            meta = json.loads(r["meta"] or "{}")
        except ValueError:
            meta = {}
        b["queries"][describe_call(r["q"], meta)] += 1
        if meta.get("program"):
            b["programs"][meta["program"]] += 1
        b["times"].append(r["created_at"])
    total = sum(b["n"] for b in by.values())
    out = []
    for b in sorted(by.values(), key=lambda b: -b["n"])[:limit]:
        ts = [datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S") for t in b["times"] if t]
        # Calls in one burst sit seconds apart; the rhythm is the gap between
        # bursts, so only gaps of a minute or more count. A loop asking eight
        # questions every five minutes has seven tiny gaps per real one.
        between = sorted(g for g in ((ts[i] - ts[i - 1]).total_seconds() for i in range(1, len(ts)))
                         if g >= 60)
        rhythm = between[len(between) // 2] if len(between) >= 3 else None
        top = sorted(b["queries"].items(), key=lambda kv: -kv[1])[:3]
        # MCP clients name themselves in the handshake; REST callers only in
        # their user agent, of which a non-browser program's name is logged.
        program = max(b["programs"].items(), key=lambda kv: kv[1])[0] if b["programs"] else None
        out.append({"v": b["v"], "client": clients.get(b["v"]) or program, "n": b["n"],
                    "share_pct": round(100 * b["n"] / total) if total else 0,
                    "channels": sorted(b["channels"]), "distinct": len(b["queries"]),
                    "top": [{"q": q, "n": n} for q, n in top],
                    "first": b["times"][0][11:16], "last": b["times"][-1][11:16],
                    "rhythm_min": round(rhythm / 60) if rhythm else None})
    return {"callers": out, "total": total, "visitors": len(by), "hours": hours}


# ---------------------------------------------------------------------------
# Growth: one summary row per day, kept after usage_log forgets the day
# ---------------------------------------------------------------------------
_DAY_SQL = """
SELECT DATE(created_at) AS day,
  COUNT(DISTINCT CASE WHEN action = 'view' AND json_extract(meta, '$.bot') = 0
                      THEN json_extract(meta, '$.v') END) AS visitors,
  SUM(action = 'view' AND COALESCE(json_extract(meta, '$.bot'), 0) = 0) AS human_views,
  SUM(action = 'search' AND channel = 'browser') AS web_searches,
  SUM(action = 'search' AND channel = 'api') AS api_searches,
  SUM(action LIKE 'tool:%') AS agent_calls,
  json_group_array(DISTINCT CASE WHEN action = 'mcp:connect'
                                 THEN json_extract(meta, '$.client') END) AS clients
FROM usage_log WHERE created_at >= ? AND created_at < ? GROUP BY day
"""


def _day_rows(conn, start: str, end: str) -> list[dict]:
    out = []
    for r in conn.execute(_DAY_SQL, (start, end)).fetchall():
        names = sorted({c for c in json.loads(r["clients"] or "[]") if c})
        out.append({"day": r["day"], "visitors": r["visitors"] or 0, "human_views": r["human_views"] or 0,
                    "web_searches": r["web_searches"] or 0, "api_searches": r["api_searches"] or 0,
                    "agent_calls": r["agent_calls"] or 0, "agent_clients": names})
    return out


def rollup_daily(conn, today: Optional[str] = None) -> int:
    """Summarise every finished day still in usage_log that is not stored yet,
    and yesterday again in case it was written to late. Never today (still
    running; computed live) and never deletes: a day that has left usage_log
    keeps its row."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    first = conn.execute("SELECT MIN(DATE(created_at)) FROM usage_log").fetchone()[0]
    if not first or first >= today:
        return 0
    stored = {r[0] for r in conn.execute("SELECT day FROM daily_usage")}
    if all(d in stored for d in _days(first, yesterday)) and yesterday in stored:
        start = yesterday
    else:
        start = first
    rows = [r for r in _day_rows(conn, start, today) if r["day"] not in stored or r["day"] >= yesterday]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_usage (day, visitors, human_views, web_searches, api_searches, "
        "agent_calls, agent_clients) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(r["day"], r["visitors"], r["human_views"], r["web_searches"], r["api_searches"],
          r["agent_calls"], json.dumps(r["agent_clients"])) for r in rows])
    conn.commit()
    return len(rows)


def _days(first: str, last: str) -> list[str]:
    d0 = datetime.strptime(first, "%Y-%m-%d")
    d1 = datetime.strptime(last, "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]


def weekly(conn, weeks: int = 12, today: Optional[str] = None) -> list[dict]:
    """Monday-to-Sunday weeks, oldest first, the current one marked partial.
    Visitors are averaged over the days that had visitor ids: ids change every
    day, so a week's unique visitors cannot be counted, only its days'."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days = {r["day"]: dict(r) for r in conn.execute("SELECT * FROM daily_usage").fetchall()}
    for r in days.values():
        r["agent_clients"] = json.loads(r["agent_clients"] or "[]")
    tomorrow = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    for r in _day_rows(conn, today, tomorrow):
        days[r["day"]] = r
    t = datetime.strptime(today, "%Y-%m-%d").date()
    this_monday = t - timedelta(days=t.weekday())
    out = []
    for i in range(weeks - 1, -1, -1):
        mon = this_monday - timedelta(weeks=i)
        ds = [days.get((mon + timedelta(days=k)).isoformat()) for k in range(7)]
        ds = [d for d in ds if d]
        measured = [d["visitors"] for d in ds if d["visitors"]]
        clients = set()
        for d in ds:
            clients.update(d["agent_clients"])
        # Our own monitoring connects on a schedule whether anyone uses the
        # site or not; /analytics leaves it out, and so does breadth here.
        from .insights import SELF_REFERENTIAL_CLIENTS
        clients -= set(SELF_REFERENTIAL_CLIENTS)
        out.append({
            "week": f"v{mon.isocalendar()[1]}", "monday": mon.isoformat(), "days": len(ds),
            "partial": i == 0, "has_data": bool(ds),
            "visitors_per_day": round(sum(measured) / len(measured)) if measured else None,
            "visitor_days": len(measured),
            "human_views": sum(d["human_views"] for d in ds),
            "web_searches": sum(d["web_searches"] for d in ds),
            "api_searches": sum(d["api_searches"] for d in ds),
            "agent_calls": sum(d["agent_calls"] for d in ds),
            "agent_clients": len(clients),
            "agent_client_names": sorted(clients),
        })
    return out


def growth_change(weeks: list[dict], key: str) -> Optional[int]:
    """Percent change, last complete week against the one before. Per-day
    figures for visitors; totals otherwise, and only between full weeks."""
    full = [w for w in weeks if not w["partial"] and w["has_data"]]
    if len(full) < 2:
        return None
    a, b = full[-2].get(key), full[-1].get(key)
    if not a or b is None:
        return None
    return round((b - a) / a * 100)
