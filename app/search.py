"""
Full-text search over `tenders` — FTS5 with a Swedish-friendly tokenizer.

The old search was a single `title LIKE '%q%' OR description LIKE '%q%'`.
That fails in three ways users hit constantly, all reproducible against real
data:

  * `LIKE` folds case for ASCII only, so `UNDERHÅLLSPLANERING` matched
    nothing while `underhållsplanering` matched four tenders.
  * A multi-word query is one contiguous substring, so `underhåll fastighet`
    found nothing despite 484 and 352 hits for the words separately, and
    `teknisk förvaltning` worked only in that exact order.
  * Swedish compounds only match downward: searching `underhåll` finds
    `underhållsplanering`, but searching the longer compound finds nothing —
    and users type the specific one.

FTS5 with `unicode61 remove_diacritics 2` fixes the first two (case folding
including Å/Ä/Ö, diacritic folding, real tokenisation) and `bm25()` gives
relevance ranking, which the old search had no notion of. The third is
handled by prefix backoff: a single long compound that matches nothing is
progressively shortened until it does.

Everything degrades safely — if the FTS index is missing or the SQLite build
lacks FTS5, `build_match()` returns None and callers fall back to LIKE.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

LOG = logging.getLogger(__name__)

FTS_TABLE = "tenders_fts"

# Tokens shorter than this are dropped from multi-word queries — they are
# almost always Swedish stop-syllables ("av", "för", "och") that match
# everything and drag the ranking down. A one-word query is never dropped.
MIN_TOKEN_LEN = 3

# How far a single compound may be shortened before we give up. Stopping at 6
# keeps "underhållsplaneringsverktyg" -> "underhållsplanering" but refuses to
# degrade a word into a meaningless stem like "und".
MIN_PREFIX_LEN = 6


def fts_available(conn: sqlite3.Connection) -> bool:
    """True if the FTS index exists and is queryable."""
    try:
        conn.execute(f"SELECT rowid FROM {FTS_TABLE} LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def _tokens(query: str) -> list[str]:
    """Split a user query into searchable tokens.

    Keeps Swedish letters and digits; drops punctuation entirely so a query
    like "drift & underhåll (2026)" behaves like "drift underhåll 2026".
    """
    raw = [t for t in re.split(r"[^0-9A-Za-zÀ-ÿ]+", query or "") if t]
    if len(raw) <= 1:
        return raw
    longer = [t for t in raw if len(t) >= MIN_TOKEN_LEN]
    return longer or raw


def _expr(tokens: list[str], op: str = "OR") -> str:
    """Build an FTS5 MATCH expression.

    Each token is quoted (so FTS5 operators inside user input are inert) and
    given a `*` so prefixes match — `förvaltning` should find
    `förvaltningsentreprenad`.
    """
    parts = ['"' + t.replace('"', '""') + '"*' for t in tokens]
    return f" {op} ".join(parts)


def _count(conn: sqlite3.Connection, expr: str) -> int:
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?", (expr,)
        ).fetchone()[0]
    except sqlite3.Error as exc:
        LOG.debug("FTS match failed for %r: %s", expr, exc)
        return -1


def build_match(conn: sqlite3.Connection, query: str) -> Optional[str]:
    """Turn a user query into an FTS5 MATCH expression that actually matches.

    Returns None when FTS is unusable or the query has nothing searchable, so
    the caller can fall back to LIKE.

    Strategy:
      1. AND across tokens — the precise reading of a multi-word query.
      2. If that finds nothing, OR across tokens and let bm25() rank. This is
         what turns "system för underhållsplanering" from zero results into
         the three genuinely relevant tenders at the top.
      3. For a single token that still finds nothing, shorten it step by step
         (compound backoff).
    """
    if not query or not query.strip():
        return None
    if not fts_available(conn):
        return None

    tokens = _tokens(query)
    if not tokens:
        return None

    if len(tokens) > 1:
        both = _expr(tokens, "AND")
        if _count(conn, both) > 0:
            return both
        loose = _expr(tokens, "OR")
        if _count(conn, loose) > 0:
            return loose
        # Every token unknown — fall through and back off the longest one,
        # which is the word carrying the meaning in a Swedish query.
        tokens = [max(tokens, key=len)]

    word = tokens[0]
    expr = _expr([word])
    n = _count(conn, expr)
    if n > 0:
        return expr
    if n < 0:
        return None                       # FTS errored; caller falls back

    # Compound backoff: "underhållsplaneringsverktyg" -> "underhållsplanering"
    trimmed = word
    while len(trimmed) > MIN_PREFIX_LEN:
        trimmed = trimmed[:-2]
        expr = _expr([trimmed])
        if _count(conn, expr) > 0:
            LOG.debug("compound backoff: %r -> %r", word, trimmed)
            return expr
    return None                           # genuinely nothing; LIKE won't help either


def match_subquery() -> str:
    """SQL fragment yielding (id, r) for a MATCH expression, ranked by bm25.

    Joined by callers so the existing source/authority/cpv/status filters and
    pagination keep working unchanged:

        FROM tenders t JOIN (<this>) m ON m.id = t.id WHERE ... ORDER BY m.r
    """
    return (f"SELECT rowid AS id, bm25({FTS_TABLE}) AS r "
            f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?")
