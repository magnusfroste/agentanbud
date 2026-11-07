"""
Tenders to expect: framework agreements that should soon be put out again.

A framework agreement under LOU may as a rule run for at most four years
(7 kap. 2 §), and most run two years with options to extend to four. So a
buyer that published a framework procurement in 2023 will, unless it gives
up the need, publish the next one between 2025 and 2027. Upphandlingsmyndig-
hetens statistics (the contracts table) give every framework awarded in
Sweden 2021–2024 with its publication date, buyer, main CPV code and the
suppliers on it; that is enough for an estimate, which the UK and US
services sell as "contract expiries" (Stotles, Tussell, HigherGov).

The window: from two to four years after the earlier procurement was
published, and the end of it is what we rank by — "at the latest" by the
four-year cap. With data up to 2024 every window has opened by now, so the
question that sorts them is which must be renewed soonest. Left out:
- frameworks whose window has passed (it was renewed, extended or dropped;
  we cannot tell which);
- those the same buyer has demonstrably put out again: a later procurement
  under the framework's own CPV prefix, in the contracts or among our notices.
  The prefix is the framework's main code without its trailing zeros, at
  most three digits: 34100000 (motor vehicles) is "341", so a new notice for
  passenger cars (34110000) is its renewal; 15000000 (food) is "15".
Marked "ute nu" when such a tender is open right now, with a link.

It is an estimate and every surface says so: the agreement may have been
extended, ended early or replaced by a call-off from someone else's
framework, and the statistics have no end dates.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app import seo

EARLIEST_YEARS, LATEST_YEARS = 2, 4
REDONE_AFTER_DAYS = 300   # a later procurement this long after counts as the renewal
MONTHS = ("januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti",
          "september", "oktober", "november", "december")
CAVEAT = ("Uppskattning: ett ramavtal får som regel löpa högst fyra år, så en ny upphandling väntas "
          "senast fyra år efter den förra, ofta tidigare. Avtalet kan ha förlängts, avslutats eller "
          "ersatts av ett annat, och vi ser inte alla nya upphandlingar.")


def _month(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.year}"


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:            # 29 February
        return d.replace(year=d.year + years, day=28)


def _parse(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


_LOCK = threading.Lock()
_CACHE: dict = {}    # db path -> (version, frameworks)


def _version(conn):
    try:
        r = conn.execute("SELECT value FROM source_state WHERE source = 'uhm_contracts'").fetchone()
        return r[0] if r else None
    except sqlite3.OperationalError:
        return None


def _frameworks(conn) -> list[dict]:
    """Every framework procurement in the contracts, once per import."""
    from app.related import buyer_match_key
    from mcp_shared import pretty_company
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    version = _version(conn)
    with _LOCK:
        hit = _CACHE.get(path)
        if hit and hit[0] == version and version is not None:
            return hit[1]
    try:
        rows = conn.execute(
            "SELECT procurement_id, MAX(buyer_name), MAX(title), MAX(cpv), MIN(published), "
            "COUNT(DISTINCT supplier_key), MAX(kind = 'Ramavtal') FROM contracts "
            "WHERE procurement_id IS NOT NULL AND published IS NOT NULL AND buyer_name IS NOT NULL "
            "GROUP BY procurement_id").fetchall()
    except sqlite3.OperationalError:
        rows = []
    # Every procurement per buyer, frameworks or not: a later one under a
    # framework's prefix is its renewal.
    by_buyer: dict = {}
    out = []
    for pid, buyer, title, cpv, published, n, framework in rows:
        d = _parse(published)
        if not d or not cpv:
            continue
        match = buyer_match_key(seo._buyer(buyer))
        by_buyer.setdefault(match, []).append((cpv, d))
        if framework:
            out.append({"id": pid, "buyer_name": buyer, "buyer": pretty_company(buyer), "match": match,
                        "title": title or "(utan titel)", "cpv": cpv, "group": _prefix(cpv), "published": d,
                        "suppliers": n})
    for f in out:
        f["redone"] = any(c.startswith(f["group"]) and (d - f["published"]).days >= REDONE_AFTER_DAYS
                          for c, d in by_buyer[f["match"]])
    with _LOCK:
        _CACHE[path] = (version, out)
    return out


def warm(path: str) -> threading.Thread:
    """Build the framework list in the background after a start (0.4 s)."""
    def run():
        from app.db import connect
        try:
            conn = connect(path)
            try:
                _frameworks(conn)
            finally:
                conn.close()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("framework list warm-up failed")
    t = threading.Thread(target=run, daemon=True, name="renewals-warm")
    t.start()
    return t


def reset() -> None:
    with _LOCK:
        _CACHE.clear()


def _prefix(cpv: str) -> str:
    """A framework's CPV prefix for matching its renewal (see the module docstring)."""
    p = cpv.rstrip("0")[:3]
    return p if len(p) >= 2 else cpv[:2]


def _incumbents(conn, pid: str, top: int = 3) -> list[dict]:
    from app import suppliers
    from mcp_shared import pretty_company
    rows = conn.execute(
        "SELECT supplier_key, MAX(supplier_name) FROM contracts WHERE procurement_id = ? AND supplier_name IS NOT NULL "
        "GROUP BY supplier_key ORDER BY COUNT(*) DESC, 2 LIMIT ?", (pid, top)).fetchall()
    return [{"name": pretty_company(name), "slug": suppliers.page_slug(conn, key)} for key, name in rows]


def _select(conn, buyer: str, cpv, within_months: int, today: Optional[date]) -> list[dict]:
    from app import related
    today = today or datetime.now(timezone.utc).date()
    months = today.month - 1 + within_months
    horizon = date(today.year + months // 12, months % 12 + 1, 1)
    prefixes = (cpv,) if isinstance(cpv, str) and cpv else tuple(cpv or ())
    want = related.buyer_match_key(seo._buyer(buyer)) if buyer else None
    snap = related._snapshot(conn)
    by_match = getattr(snap, "by_match", {})
    now_iso = related._now_iso()
    picked = []
    for f in _frameworks(conn):
        if f["redone"] or (want and f["match"] != want):
            continue
        if prefixes and not any(f["cpv"].startswith(p) for p in prefixes):
            continue
        earliest = _add_years(f["published"], EARLIEST_YEARS)
        latest = _add_years(f["published"], LATEST_YEARS)
        # Due by the four-year cap within the horizon. Every framework from
        # 2021–2024 is past its earliest date by now, so "could come" says
        # nothing; "must come by" does.
        if latest < today or latest > horizon:
            continue
        key = by_match.get(f["match"])
        b = snap.buyers.get(key) if key else None
        item = {**f, "earliest": earliest, "latest": latest, "open_id": None, "open_title": None,
                "buyer_slug": b.get("slug") if b else None}
        renewed_after = (f["published"] + timedelta(days=REDONE_AFTER_DAYS)).isoformat()
        if b and b["latest"].get(f["group"], "") >= renewed_after:
            # Our notices hold a later one in the group: open now, show it;
            # closed, the framework has been renewed already.
            open_now = [i for i in b["open_ids"] if snap.items[i][2] > now_iso
                        and any(c.startswith(f["group"]) for c in snap.items[i][4])]
            if not open_now:
                continue
            item["open_id"], item["open_title"] = open_now[0], snap.items[open_now[0]][0]
        picked.append(item)
    picked.sort(key=lambda x: (x["open_id"] is None, x["latest"], x["buyer"]))
    return picked


def expected(conn, *, buyer: str = "", cpv=(), within_months: int = 24,
             limit: int = 50, today: Optional[date] = None) -> list[dict]:
    """Frameworks expected to be tendered again: those out now first, then
    the ones nearest the end of their window."""
    out = []
    for x in _select(conn, buyer, cpv, within_months, today)[:limit]:
        out.append({"id": x["id"], "title": x["title"], "buyer": x["buyer"], "buyer_slug": x["buyer_slug"],
                    "cpv": x["cpv"], "published": x["published"].isoformat(),
                    "published_text": _month(x["published"]), "suppliers": x["suppliers"],
                    "incumbents": _incumbents(conn, x["id"]),
                    "window": f"{_month(x['earliest'])}–{_month(x['latest'])}",
                    "latest_text": _month(x["latest"]),
                    "earliest": x["earliest"].isoformat(), "latest": x["latest"].isoformat(),
                    "open_id": x["open_id"], "open_title": x["open_title"]})
    return out


def count(conn, *, buyer: str = "", cpv=(), within_months: int = 12, today: Optional[date] = None) -> int:
    return len(_select(conn, buyer, cpv, within_months, today))
