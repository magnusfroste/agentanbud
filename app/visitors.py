"""
Unique visitors, without knowing who they are.

Behind the Cloudflare tunnel every request reaches the app from the same local
address, so the server cannot tell one visitor from a thousand. Cloudflare
puts the real address in CF-Connecting-IP. We never store it. A visitor is

    sha256(salt_of_the_day | address | user agent)[:12]

and the salt is random, kept in the database for one day, and deleted when the
day is over. The same visitor on the same day gives the same id, so a day's
unique visitors can be counted; tomorrow the salt is new, so nobody can be
followed from one day to the next, and once yesterday's salt is gone not even
we can recompute yesterday's ids. No raw address reaches the logs, the
database or any page.

The same id is attached to searches and MCP tool calls, which is what lets
/analytics count a search term once per searcher per day instead of once per
call: an agent that asks the same question a hundred times is one searcher.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

_lock = threading.Lock()
_cache: dict = {"day": None, "salt": None}

# How often the header was there, for /ops: if Cloudflare stops sending it,
# every visitor silently becomes "unknown" and the count reads zero.
HEADER = {"seen": 0, "missing": 0}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _salt(conn, day: str) -> str:
    with _lock:
        if _cache["day"] == day and _cache["salt"]:
            return _cache["salt"]
    conn.execute("INSERT OR IGNORE INTO visitor_salt (day, salt) VALUES (?, ?)",
                 (day, secrets.token_hex(16)))
    # Yesterday's salt goes the moment today's exists: from then on, nobody
    # can recompute yesterday's ids, not even with the database in hand.
    conn.execute("DELETE FROM visitor_salt WHERE day < ?", (day,))
    conn.commit()
    salt = conn.execute("SELECT salt FROM visitor_salt WHERE day = ?", (day,)).fetchone()[0]
    with _lock:
        _cache.update(day=day, salt=salt)
    return salt


def visitor_id(conn, address: Optional[str], user_agent: Optional[str],
               day: Optional[str] = None) -> Optional[str]:
    if not address:
        return None
    salt = _salt(conn, day or _today())
    raw = f"{salt}|{address.strip()}|{(user_agent or '')[:300]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def address_of(headers) -> Optional[str]:
    """The visitor's address as Cloudflare reports it, counted for /ops."""
    a = headers.get("cf-connecting-ip")
    HEADER["seen" if a else "missing"] += 1
    return a
