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

What passes is stored in learned_terms as "suggested", or "active" when
LLM_SYNONYMS_MODE=auto. Only active rows are searched with. /api/ops/synonyms
lists them, and an admin can activate or reject each one.

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
    from app.search import build_match, match_subquery
    a = _fold(alt)
    if a == _fold(term):
        return False, 0, "samma som sökningen"
    if len(a.replace(" ", "")) < MIN_LEN or not _WORD.match(a) or len(a.split()) > 3:
        return False, 0, "för kort eller inte ett ord"
    match = build_match(conn, a)
    if not match:
        return False, 0, "finns inte i datan"
    total = conn.execute(f"SELECT COUNT(*) FROM ({match_subquery()})", [match]).fetchone()[0]
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


def store(conn, term: str, alt: str, status: str, open_hits: int, reason: str, model: str) -> None:
    conn.execute(
        "INSERT INTO learned_terms (term, alt, status, open_hits, reason, model) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(term, alt) DO UPDATE SET "
        "open_hits = excluded.open_hits, reason = excluded.reason, updated_at = CURRENT_TIMESTAMP",
        (_fold(term), _fold(alt), status, open_hits, reason, model))


def set_status(conn, term: str, alt: str, status: str) -> bool:
    n = conn.execute("UPDATE learned_terms SET status = ?, updated_at = CURRENT_TIMESTAMP "
                     "WHERE term = ? AND alt = ?", (status, _fold(term), _fold(alt))).rowcount
    conn.commit()
    reload()
    return n > 0


def listing(conn, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT term, alt, status, open_hits, reason, model, created_at, updated_at "
        "FROM learned_terms ORDER BY created_at DESC LIMIT ?", (limit,))]
