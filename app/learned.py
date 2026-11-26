"""
Synonyms learned at night: searches that found nothing, the words the notices
use instead, checked against the data before anything is used.

By hand this was: read the unmet-demand list on /analytics, guess what a
procurement officer writes ("nödström" is "reservkraft", "chatbot" is
"chattfunktion"), add it to app/query_terms.py, test, deploy. scraper/synonyms.py
now does the first two each night with a language model, and this module does
the part that must not be left to a model — deciding whether a suggestion is
safe to search with:

  * a word of at least MIN_LEN letters, at most three words, not the search
    itself;
  * it finds open tenders;
  * it is not so broad that it finds a tenth of the database (MAX_HITS);
  * at least half of what it finds is in the CPV division the model says the
    search belongs to. That is the check "pall" would have failed: its
    matches were palliative care (85) where the search was pallets (44).

A word that is right but finds nothing open yet is stored "dormant" and
checked again each night (wake). What passes is stored in learned_terms as "active" (LLM_SYNONYMS_MODE=auto,
the default) or "suggested" (=suggest). Only active rows are searched with.
An agent reviews them now and then over MCP (list_synonyms, review_synonym
with the admin key), or a person on /ops/synonymer, and rejects what the
checks let through but is too broad.

app/query_terms.swedish_for reads the active ones through lookup(), from a
cache reloaded every RELOAD_S — never a database read per search.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

LOG = logging.getLogger(__name__)

MIN_LEN = 5
MAX_HITS = 3000
MIN_DIVISION_SHARE = 0.5
MAX_PER_TERM = 4
RELOAD_S = 600

DB_PATH: Optional[str] = None          # set by app.main.create_app

_lock = threading.Lock()
_cache: dict[str, tuple[str, ...]] = {}
_loaded_at = 0.0
_version = 0


def _fold(text: str) -> str:
    return " ".join((text or "").casefold().split())


def reload(db_path: Optional[str] = None) -> None:
    """Read the active learned terms into the cache."""
    global _cache, _loaded_at, _version
    path = db_path or DB_PATH
    fresh: dict[str, list[str]] = {}
    if path:
        try:
            from app.db import connect
            conn = connect(path)
            try:
                for r in conn.execute("SELECT term, alt FROM learned_terms WHERE status = 'active' "
                                      "ORDER BY term, open_hits DESC"):
                    fresh.setdefault(r["term"], []).append(r["alt"])
            finally:
                conn.close()
        except Exception:
            LOG.debug("learned terms not loaded", exc_info=True)
    with _lock:
        new = {k: tuple(v) for k, v in fresh.items()}
        if new != _cache:
            _version += 1
        _cache, _loaded_at = new, time.time()


def _maybe_reload() -> None:
    if time.time() - _loaded_at > RELOAD_S:
        reload()


def lookup(term: str) -> tuple[str, ...]:
    """The active learned alternatives for a search word or phrase."""
    _maybe_reload()
    return _cache.get(_fold(term), ())


def version() -> int:
    """Changes whenever the active set does: part of cache keys that depend on it."""
    _maybe_reload()
    return _version


# --- Checking a suggestion ------------------------------------------------

_WORD = re.compile(r"^[a-zåäöéü][a-zåäöéü\- ]*$")


def _open_sql() -> str:
    return ("source_system != 'ted_awards' AND (deadline > ? OR (deadline IS NULL AND "
            "(source_system = 'lov' OR published_at IS NULL OR published_at >= date('now', '-60 days'))))")


def check(conn, term: str, alt: str, division: Optional[str]) -> tuple[bool, int, str]:
    """(usable, open hits, why) for one suggested alternative."""
    from datetime import datetime
    from app.search import CONTENT_COLUMNS, _quote, fts_available, match_subquery
    a = _fold(alt)
    if a == _fold(term):
        return False, 0, "samma som sökningen"
    if len(a.replace(" ", "")) < MIN_LEN or not _WORD.match(a) or len(a.split()) > 3:
        return False, 0, "för kort eller inte ett ord"
    if not fts_available(conn):
        return False, 0, "sökindexet saknas"
    # Counted the way search uses a synonym (app/search._term): the word as it
    # is, a prefix, in titles and descriptions. Not build_match, which
    # shortens a word that finds nothing: on 25 November 'förvaringslådor'
    # passed with one hit that was 'förvaringslösningar', and then found
    # nothing when searched with.
    match = f"{CONTENT_COLUMNS} : ({_quote(a)})"
    total = conn.execute(f"SELECT COUNT(*) FROM ({match_subquery()})", [match]).fetchone()[0]
    if not total:
        return False, 0, "finns inte i datan"
    if total > MAX_HITS:
        return False, 0, f"för brett ({total} träffar)"
    now = datetime.now().isoformat(timespec="seconds")
    open_n = conn.execute(
        f"SELECT COUNT(*) FROM tenders JOIN ({match_subquery()}) m ON m.id = tenders.id "
        f"WHERE {_open_sql()}", [match, now]).fetchone()[0]
    if not open_n:
        return False, 0, "inga öppna upphandlingar"
    div = "".join(ch for ch in str(division or "") if ch.isdigit())[:2]
    if len(div) == 2:
        rows = conn.execute(
            f"SELECT cpv_codes FROM tenders JOIN ({match_subquery()}) m ON m.id = tenders.id "
            f"WHERE cpv_codes IS NOT NULL AND cpv_codes != '[]' LIMIT 200", [match]).fetchall()
        coded = 0
        inside = 0
        for (codes,) in rows:
            try:
                cs = [str(c) for c in json.loads(codes or "[]")]
            except Exception:
                continue
            if not cs:
                continue
            coded += 1
            inside += any(c.startswith(div) for c in cs)
        if coded >= 3 and inside / coded < MIN_DIVISION_SHARE:
            return False, open_n, f"fel bransch ({inside} av {coded} i CPV {div})"
    return True, open_n, "ok"


# A word that is right but finds nothing open tonight is not wrong: it waits
# ("dormant") and is checked again each night, and searched with the night an
# open notice uses it. On 26 November 'laddskåp' → 'laddstation' was rejected
# for good only because no charging station was out for tender that day.
DORMANT_REASONS = ("inga öppna upphandlingar", "finns inte i datan")
DORMANT_DAYS = 180


def outcome(ok: bool, why: str, status_if_ok: str) -> str:
    """The status a checked suggestion gets."""
    if ok:
        return status_if_ok
    return "dormant" if why.startswith(DORMANT_REASONS) else "rejected"


def wake(conn, status_if_ok: str) -> int:
    """Check the dormant suggestions again; those that now pass get
    status_if_ok. Returns how many woke. Rejections nobody reviewed that are
    only 'nothing open' become dormant first (rows from before this existed);
    a status set by a person or an agent is never touched."""
    conn.execute(
        "UPDATE learned_terms SET status = 'dormant' WHERE status = 'rejected' AND COALESCE(reviewed, 0) = 0 "
        "AND alt != '' AND (" + " OR ".join("reason LIKE ?" for _ in DORMANT_REASONS) + ")",
        [f"{r}%" for r in DORMANT_REASONS])
    rows = conn.execute(
        "SELECT term, alt, reason FROM learned_terms WHERE status = 'dormant' AND COALESCE(reviewed, 0) = 0 "
        "AND created_at >= datetime('now', ?)", (f"-{DORMANT_DAYS} days",)).fetchall()
    woke = 0
    for r in rows:
        m = re.search(r"CPV (\d\d)", r["reason"] or "")
        division = m.group(1) if m else ""
        ok, hits, why = check(conn, r["term"], r["alt"], division)
        status = outcome(ok, why, status_if_ok)
        conn.execute("UPDATE learned_terms SET status = ?, open_hits = ?, reason = ?, updated_at = CURRENT_TIMESTAMP "
                     "WHERE term = ? AND alt = ?",
                     (status, hits, f"{why}; CPV {division or '?'}", r["term"], r["alt"]))
        woke += status == status_if_ok
    conn.commit()
    return woke


def store(conn, term: str, alt: str, status: str, open_hits: int, reason: str, model: str) -> None:
    conn.execute(
        "INSERT INTO learned_terms (term, alt, status, open_hits, reason, model) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(term, alt) DO UPDATE SET "
        "open_hits = excluded.open_hits, reason = excluded.reason, updated_at = CURRENT_TIMESTAMP",
        (_fold(term), _fold(alt), status, open_hits, reason, model))


class Refused(ValueError):
    """An activation the checks do not allow; the message says why."""


def set_status(conn, term: str, alt: str, status: str) -> bool:
    """Set a status by hand (a person on /ops, an agent over MCP or REST).
    False when there is no such suggestion.

    Activating one is checked again, against today's data: one that finds
    its hits in the wrong CPV division, or so many that it would swamp the
    search, or is not a word, raises Refused instead. On 25 November an
    agent activated 'teambuilding' → 'utbildning' (77 open tenders, 43 of
    200 in the division) and 'suveränitet' → 'myndighetsutövning' (0 of 14)
    over the checks' verdict. A word that only finds nothing open yet may
    be activated; it does no harm and finds its notices when they come."""
    t, a = _fold(term), _fold(alt)
    row = conn.execute("SELECT reason FROM learned_terms WHERE term = ? AND alt = ?", (t, a)).fetchone()
    if row is None:
        return False
    if status == "active":
        m = re.search(r"CPV (\d\d)", row[0] or "")
        division = m.group(1) if m else ""
        ok, hits, why = check(conn, t, a, division)
        if not ok and not why.startswith(DORMANT_REASONS):
            raise Refused(f"{why}; CPV {division or '?'}")
        conn.execute("UPDATE learned_terms SET open_hits = ?, reason = ? WHERE term = ? AND alt = ?",
                     (hits, f"{why}; CPV {division or '?'}", t, a))
    conn.execute("UPDATE learned_terms SET status = ?, reviewed = 1, updated_at = CURRENT_TIMESTAMP "
                 "WHERE term = ? AND alt = ?", (status, t, a))
    conn.commit()
    reload()
    return True


def listing(conn, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT term, alt, status, open_hits, reason, model, created_at, updated_at "
        "FROM learned_terms ORDER BY created_at DESC LIMIT ?", (limit,))]
