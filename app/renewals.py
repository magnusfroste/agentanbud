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
  in the same CPV group (the first three digits of the main code) whose
  title shares a word with the framework's (related.title_stems), in the
  contracts or among our notices.
The CPV group alone is not enough. A framework coded at the top of a
division (71000000) matched everything in it, and in production (8 Nov
2026) all 150 frameworks on the page were marked "out now", linked to
unrelated tenders — laboratory analyses for "technical consultants for
early stages". Requiring a shared word in the title costs some renewals
that are worded differently, which then show as still expected: the
safer mistake for an estimate.
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
    from app.related import title_stems
    by_buyer: dict = {}
    out = []
    for pid, buyer, title, cpv, published, n, framework in rows:
        d = _parse(published)
        if not d or not cpv:
            continue
        match = buyer_match_key(seo._buyer(buyer))
        stems = title_stems(title)
        by_buyer.setdefault(match, []).append((cpv, d, stems))
        if framework:
            out.append({"id": pid, "buyer_name": buyer, "buyer": pretty_company(buyer), "match": match,
                        "title": title or "(utan titel)", "cpv": cpv, "group": _prefix(cpv), "published": d,
                        "suppliers": n, "stems": stems})
    for f in out:
        f["redone"] = any(c.startswith(f["group"]) and (d - f["published"]).days >= REDONE_AFTER_DAYS
                          and f["stems"] & st for c, d, st in by_buyer[f["match"]])
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
    """A framework's CPV group for matching its renewal: the first three digits."""
    return cpv[:3]


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
        later = [(pub, i) for pub, st, i in (b["history"].get(f["group"], []) if b else [])
                 if pub >= renewed_after and f["stems"] & st]
        if later:
            # Our notices hold its renewal: open now, show it; closed, it has
            # been renewed already.
            open_now = sorted((pub, i) for pub, i in later if i in snap.items and snap.items[i][2] > now_iso)
            if not open_now:
                continue
            i = open_now[-1][1]
            item["open_id"], item["open_title"] = i, snap.items[i][0]
        picked.append(item)
    picked.sort(key=lambda x: (x["open_id"] is None, x["latest"], x["buyer"]))
    # One open tender is the renewal of one framework, the first listed.
    seen, out = set(), []
    for x in picked:
        if x["open_id"] is not None:
            if x["open_id"] in seen:
                x = {**x, "open_id": None, "open_title": None}
            seen.add(x["open_id"])
        out.append(x)
    out.sort(key=lambda x: (x["open_id"] is None, x["latest"], x["buyer"]))
    return out


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
