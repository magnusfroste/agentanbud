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
from functools import lru_cache
import sqlite3
from typing import Optional

from app.query_terms import swedish_for

LOG = logging.getLogger(__name__)

FTS_TABLE = "tenders_fts"

# Tokens shorter than this are dropped from multi-word queries — they are
# almost always Swedish stop-syllables ("av", "för", "och") that match
# everything and drag the ranking down. A one-word query is never dropped.
MIN_TOKEN_LEN = 3

# Short words that are not stop-syllables: they are what is being bought.
# Dropping "IT" turned "IT-konsult stockholm" into a search for any
# consultant — electrical inspection, wolf counts, kitchen refrigeration.
# A short word written in capitals in the query (an acronym) is kept too.
KEEP_SHORT = {"it", "ai", "ux", "ui", "va", "hr", "eu", "pr", "el", "ip"}

# Terms this short are matched as whole words, not as prefixes: "nis" (from
# "NIS 2") matched "Nissan" and "ai" matched every word that starts with it.
# Longer terms keep the prefix, because a Swedish search needs it.
EXACT_MAX_LEN = 3

# Below this length the LIKE recall floor is not used: '%nis%' is inside
# "organisation" and '%ai%' inside "mail". The floor exists to find a word at
# the tail of a compound ("fastighetsunderhåll"), which a short query is not.
LIKE_MIN_LEN = 5

# Where the words of a search are looked for first. The buyer's name is
# searched only when no title or description has them — then the query is
# most likely a buyer ("Trafikverket"), and callers can say so. Before this,
# "meteorologiska" answered with SMHI's fruit baskets, because the word is in
# the institute's name.
CONTENT_COLUMNS = "{title description}"
BUYER_COLUMNS = "{authority}"

# How far a single compound may be shortened before we give up. Stopping at 6
# keeps "underhållsplaneringsverktyg" -> "underhållsplanering" but refuses to
# degrade a word into a meaningless stem like "und".
MIN_PREFIX_LEN = 6


def _fold(word: str) -> str:
    return (word.casefold().replace("å", "a").replace("ä", "a").replace("ö", "o")
            .replace("é", "e"))


def _places() -> dict:
    """Folded place word → (region LIKE, authority LIKE). The counties from
    app/hubs.py, in the forms people type ("stockholm", "stockholms"), and the
    two largest cities that are not county names."""
    from app.hubs import COUNTIES
    out = {}
    for hub in COUNTIES:
        region = hub.name.replace(" län", "")
        if " " in region:
            continue                       # "Västra Götaland": see göteborg below
        root = region[:-1] if region.endswith("s") and not region.endswith("ss") else region
        for w in (root, root + "s"):
            out[_fold(w)] = (root, root)
    out["goteborg"] = ("Västra Götaland", "Göteborg")
    out["malmo"] = ("Skåne", "Malmö")
    return out


_PLACES: Optional[dict] = None


def split_place(query: str) -> tuple[str, Optional[tuple[str, str]]]:
    """Take a place out of a search: "IT-konsult stockholm" is a search for
    IT consultants in Stockholm, not for tenders that mention the word. Returns
    (the query without it, (region word, buyer word)) or (query, None). A query
    that is only a place is left alone — then the place is what is searched."""
    global _PLACES
    if _PLACES is None:
        _PLACES = _places()
    words = (query or "").split()
    place, rest = None, []
    for w in words:
        key = _fold(re.sub(r"[^0-9A-Za-zÀ-ÿ]+", "", w))
        if place is None and key in _PLACES:
            place = _PLACES[key]
        elif key == "lan" and place is not None:
            continue
        else:
            rest.append(w)
    if place is None or not rest:
        return query, None
    return " ".join(rest), place


def place_sql() -> str:
    """WHERE fragment for split_place()'s result: in that county, or bought by
    a buyer with the place in its name (most TED notices say only "Sverige")."""
    return "(region LIKE ? OR authority LIKE ?)"


# --- What is bought, by its CPV name -------------------------------------
# The notices often do not say in words what the CPV code says: a tender for
# "Aggregat till vattenverk" is coded 31682510 Reservkraftssystem. A search
# word that names a CPV code finds the tenders with that code too, after the
# ones that have the word. Only specific words do: a word that names more
# than CPV_MAX_CODES codes ("tjänster": 215, "programvara": 144) says nothing
# about which, and short words ("väder" → windmills, weather insurance) are
# too loose.
CPV_MIN_WORD = 6
CPV_MAX_CODES = 12
CPV_MAX_PREFIXES = 4


def _cpv_labels() -> dict:
    from app.related import _labels
    return _labels()


def cpv_for_query(query: str) -> tuple[tuple[str, str], ...]:
    """cpv_for_query_cached() for the current set of learned synonyms."""
    from app import learned
    return _cpv_for_query(query, learned.version())


@lru_cache(maxsize=2048)
def _cpv_for_query(query: str, _learned_version: int) -> tuple[tuple[str, str], ...]:
    """[(code prefix, Swedish name)] for the CPV codes a search names.

    A one-word search, or any of its Swedish equivalents, of at least
    CPV_MIN_WORD letters, names a code when it starts a word of the code's
    name. A search of several words names a code only when every word does:
    "bärbara datorer" is 30213100, not portable lamps and radios. The prefix
    is the code without its trailing zeros (90910000 → "9091"), so the
    branch below it counts, and a code under another one is left out. A
    parent named "X och Y" that only matched for one of them gives way to
    its matching children: "meteorologiska" is 3812 Meteorologiska
    instrument, not all of 381 Navigationsinstrument och meteorologiska
    instrument."""
    tokens = [t.casefold() for t in _tokens(query)]
    if not tokens:
        return ()
    if len(tokens) == 1:
        groups = [[w.casefold()] for w in (tokens[0], *swedish_for(tokens[0]))
                  if len(w) >= CPV_MIN_WORD and " " not in w]
    else:
        groups = [tokens] if all(len(t) >= 4 for t in tokens) else []
    if not groups:
        return ()
    labels = _cpv_labels()
    found: dict[str, str] = {}
    for words in groups:
        # The search's own word may start any word of the name. A Swedish
        # equivalent must start the name itself: "lastpall" is 44423330
        # Lastpallar, not 42998000 Lyftanordning för lastpallar.
        own = words == groups[0] and (len(tokens) > 1 or words[0] == tokens[0])
        lead = r"(^|[\s\-(/,])" if own else r"^"
        pats = [re.compile(lead + re.escape(w)) for w in words]
        codes = {c: l for c, l in labels.items() if all(p.search(l.casefold()) for p in pats)}
        if not codes or len(codes) > CPV_MAX_CODES:
            continue
        prefixes = {}
        for c, l in codes.items():
            prefix = c.rstrip("0")
            prefixes[prefix if len(prefix) >= 3 else c[:3]] = l
        for p, l in prefixes.items():
            compound = " och " in l.casefold() or "," in l
            if compound and any(q != p and q.startswith(p) for q in prefixes):
                continue
            found.setdefault(p, l)
    out = []
    for p in sorted(found, key=len):        # the broadest first: "9091" covers "90911"
        if not any(p.startswith(q) for q, _ in out):
            out.append((p, found[p]))
    return tuple(out[:CPV_MAX_PREFIXES])


def cpv_sql(n: int) -> str:
    """OR-able fragment matching any of n CPV prefixes in cpv_codes (a JSON
    array of strings): bind '%"<prefix>%' for each."""
    return " OR ".join(["cpv_codes LIKE ?"] * n)


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
    longer = [t for t in raw if len(t) >= MIN_TOKEN_LEN or t.casefold() in KEEP_SHORT
              or (t.isalpha() and t.isupper() and len(t) > 1)]
    return longer or raw


def _quote(term: str) -> str:
    """One term or phrase, quoted (so FTS5 operators inside user input are
    inert) and given a `*` so prefixes match — `förvaltning` should find
    `förvaltningsentreprenad`."""
    star = "*" if (" " in term or len(term) > EXACT_MAX_LEN) else ""
    return '"' + term.replace('"', '""') + '"' + star


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
    if _SCOPE.cols:
        expr = f"{_SCOPE.cols} : ({expr})"
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?", (expr,)
        ).fetchone()[0]
    except sqlite3.Error as exc:
        LOG.debug("FTS match failed for %r: %s", expr, exc)
        return -1


class _Scope:
    """The column set _count() restricts to while _build() runs."""
    cols = ""


_SCOPE = _Scope()


def build_match_scoped(conn: sqlite3.Connection, query: str) -> tuple[Optional[str], str]:
    """(MATCH expression, where it matched): "content" when the words are in a
    title or description, "buyer" when only a buyer's name has them, and
    (None, "") when nothing does. The expression carries its column filter,
    so it can be used with match_subquery() as it is."""
    if not query or not query.strip() or not fts_available(conn):
        return None, ""
    tokens = _tokens(query)
    if len(tokens) > 1:
        # All the words together in a buyer's name and nowhere in one title or
        # description: a search for that buyer ("Sveriges meteorologiska"),
        # not for whichever single word a title happens to share with it.
        both = _expr(tokens, "AND")
        if (_count(conn, f"{CONTENT_COLUMNS} : ({both})") == 0
                and _count(conn, f"{BUYER_COLUMNS} : ({both})") > 0):
            return f"{BUYER_COLUMNS} : ({both})", "buyer"
    for cols, scope in ((CONTENT_COLUMNS, "content"), (BUYER_COLUMNS, "buyer")):
        _SCOPE.cols = cols
        try:
            expr = _build(conn, query)
        finally:
            _SCOPE.cols = ""
        if expr:
            return f"{cols} : ({expr})", scope
    return None, ""


def build_match(conn: sqlite3.Connection, query: str) -> Optional[str]:
    """build_match_scoped() without saying where it matched."""
    return build_match_scoped(conn, query)[0]


def like_pattern(query: str) -> str:
    """The LIKE floor's pattern for a query, or one that matches nothing when
    the query is too short for a substring match to mean anything."""
    q = (query or "").strip()
    return f"%{q}%" if len(q) >= LIKE_MIN_LEN else "\x00 no floor \x00"


def _build(conn: sqlite3.Connection, query: str) -> Optional[str]:
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
        # Every word unknown. Shortening one of them is not a fix here: it
        # turned "artificiell intelligens" into "intell" and answered with
        # stage lighting ("intelligent ljusstyrning"). A one-word compound is
        # still backed off below; several unknown words are no match.
        return None

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
