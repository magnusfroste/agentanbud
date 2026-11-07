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

from app.query_terms import swedish_for

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


def _quote(term: str) -> str:
    """One term or phrase, quoted (so FTS5 operators inside user input are
    inert) and given a `*` so prefixes match — `förvaltning` should find
    `förvaltningsentreprenad`."""
    return '"' + term.replace('"', '""') + '"*'


def _term(token: str) -> str:
    """A token, or the token OR its Swedish equivalents when it is a German or
    English word we know (app/query_terms.py): "wetterdaten" also searches
    "väderdata", "väderprognos" and "meteorolog"."""
    alts = [token] + [a for a in swedish_for(token) if a.casefold() != token.casefold()]
    if len(alts) == 1:
        return _quote(token)
    return "(" + " OR ".join(_quote(a) for a in alts) + ")"


def _expr(tokens: list[str], op: str = "OR") -> str:
    """Build an FTS5 MATCH expression, one _term per token."""
    return f" {op} ".join(_term(t) for t in tokens)


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

    # A whole phrase we know in another language — "circular business model"
    # — is searched as that phrase OR its Swedish equivalents, before the
    # words are taken one by one.
    if len(tokens) > 1:
        alts = swedish_for(" ".join(tokens))
        if alts:
            phrase = "(" + " OR ".join(_quote(a) for a in [" ".join(tokens), *alts]) + ")"
            if _count(conn, phrase) > 0:
                return phrase

    if len(tokens) > 1:
        both = _expr(tokens, "AND")
        if _count(conn, both) > 0:
            return both
        # No document has all the words. Rather than OR everything — which
        # matched a third of the database for "system för underhållsplanering"
        # and made the result count meaningless — keep only the most selective
        # token. That is the word that actually narrows the search: for that
        # query it is "underhållsplanering", giving the four right tenders.
        counted = [(c, t) for t in tokens for c in (_count(conn, _expr([t])),) if c > 0]
        if counted:
            return _expr([min(counted)[1]])
        # Every token unknown — back off the longest one, which carries the
        # meaning in a Swedish compound query.
        tokens = [max(tokens, key=len)]

    word = tokens[0]
    expr = _expr([word])
    n = _count(conn, expr)
    if n > 0:
        return expr
    if n < 0:
        return None                       # FTS errored; caller falls back

    # Compound backoff: "underhållsplaneringsverktyg" -> "underhållsplanering".
    # One letter at a time: two at a time skipped past MIN_PREFIX_LEN on an
    # odd-length word — "väderdata" (9) went 7, then 5, "väder", and answered
    # a search for weather data with bus-shelter glazing ("väderskydd").
    trimmed = word
    while len(trimmed) > MIN_PREFIX_LEN:
        trimmed = trimmed[:-1]
        expr = _expr([trimmed])
        if _count(conn, expr) > 0:
            LOG.debug("compound backoff: %r -> %r", word, trimmed)
            return expr
    return None                           # genuinely nothing; LIKE won't help either


# bm25 weight per indexed column (title, description, authority). Equal
# weights let the buyer's name outrank what is bought: "meteorolog" and
# "hydrologi" put SMHI's "Personalsystem" and "Fruktkorgar" first — the words
# are in the institute's name — above "Konsulter hydrologi och hydrogeologi"
# (November 2026). A buyer's name still matches, so "SMHI" still lists its
# tenders, but a word in the title or the description now comes first.
BM25_WEIGHTS = (10.0, 3.0, 1.0)


def match_subquery() -> str:
    """SQL fragment yielding (id, r) for a MATCH expression, ranked by bm25.

    LEFT JOINed by callers, with the old LIKE kept as an OR in the WHERE, so
    the result set is the union of both and recall can only go up:

        FROM tenders LEFT JOIN (<this>) m ON m.id = tenders.id
        WHERE (m.id IS NOT NULL OR title LIKE ? OR description LIKE ?)
        ORDER BY (m.r IS NULL), m.r

    The union matters because FTS5 prefix terms match from the start of a
    token: searching `underhåll` finds `underhållsavtal` but not
    `fastighetsunderhåll`, where the word is the tail of a compound. Plain
    LIKE finds those, and dropping to FTS alone silently lost 60 tenders for
    that one query. FTS decides the order, LIKE guarantees the floor.
    """
    return (f"SELECT rowid AS id, bm25({FTS_TABLE}, {', '.join(map(str, BM25_WEIGHTS))}) AS r "
            f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?")


def like_floor_sql(column_a: str = "title", column_b: str = "description") -> str:
    """WHERE fragment pairing the FTS join with the LIKE recall floor."""
    return f"(m.id IS NOT NULL OR {column_a} LIKE ? OR {column_b} LIKE ?)"


def rank_order_sql() -> str:
    """ORDER BY fragment: FTS hits in relevance order, LIKE-only hits after."""
    return "(m.r IS NULL), m.r"
