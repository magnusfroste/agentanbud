"""
Values and logic shared by the MCP transports and the web app.

mcp_server.py (stdio) and mcp_http.py (Streamable HTTP) each declare their own
tool schemas. That's fine for prose, but enum values are facts about the data
and had already drifted: the stdio server advertised source
["mercell", "ted"] while the HTTP server advertised all five, so an agent got
a different answer depending on how it connected. Anything enumerable that
both must agree on belongs here — and, since the same drift happened to the
usage figures, any aggregation more than one surface computes.
"""
from __future__ import annotations

# Data sources present in the tenders table. Keep in step with
# scraper/orchestrator.py's registry.
SOURCES = ["mercell", "ted", "ted_awards", "ted_pin", "lov", "kommersannons"]

BUYER_TYPES = ["municipal", "regional", "state", "unknown"]

BUYER_TYPE_DESCRIPTION = (
    "Buyer kind: 'municipal' (a municipality, its administrations or a company "
    "it owns), 'regional' (a region/landsting or its companies), 'state' "
    "(national agencies and universities), 'unknown' (~11%, includes private "
    "companies). Derived from the buyer's name and registered town — use it to "
    "search only local procurement, since a national agency's town is a head "
    "office and says nothing about where the work is."
)

SOURCE_DESCRIPTION = (
    "Data source filter. 'mercell' = Swedish tenders, many below EU thresholds, "
    "'ted' = EU-threshold notices, 'ted_awards' = awarded contracts, 'ted_pin' = "
    "planned procurements, 'lov' = LOV services, 'kommersannons' = mostly "
    "municipal tenders not published elsewhere."
)


# --- Awards / winners -------------------------------------------------------
# Three surfaces answer "who wins here?": the MCP get_winner_history tool, the
# REST /api/winners endpoint, and the dashboard panel. They had three copies of
# this loop. One place, so a correction lands everywhere at once.

# TED publishes some award notices twice under different publication numbers:
# same title, buyer, winners, value and often the same day, differing only in
# the notice id. Counting both inflates a supplier's win count — measured on IT
# awards (CPV 72), WSP went from 20 wins to 35 and Sweco from 18 to 30, while
# suppliers whose contracts are not republished were untouched. That skews the
# ranking toward whoever happens to get duplicated, which is the one thing this
# figure must not do.
#
# Value is part of the key on purpose. 76 of the 78 duplicate groups carry an
# identical value; the other 2 differ, and those are separate lots of one
# framework rather than a republication. Keying on value keeps them apart.
AWARD_DEDUP_KEY = "GROUP BY title, authority, winner_name, value"


def canonical_supplier(name: str) -> str:
    """Key for grouping supplier names that differ only in spelling.

    TED records the same company several ways. "SWECO Sverige AB" and "Sweco
    Sverige AB" are 145 and 60 wins of one supplier — case alone split the
    country's second-largest consultancy in two and pushed it down the
    ranking this module exists to produce.

    Deliberately conservative: case, the Aktiebolag/AB spelling, punctuation
    and whitespace only. Names differing by a real word are left apart —
    "Ramboll" and "Ramboll Sweden AB" may well be one company, but that is a
    guess about legal entities, and merging on a guess is how a ranking starts
    reporting something nobody can check.
    """
    import re
    n = (name or "").strip().lower()
    n = re.sub(r"\baktiebolag\b", "ab", n)
    n = re.sub(r"[.,()]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# --- Contracts awarded in Sweden (Upphandlingsmyndigheten) -----------------
# A second answer to "who wins here?", from the contracts table
# (scraper/uhm_contracts.py): every registered advertising database, below the
# EU thresholds too, 2021 onwards, published yearly. Kept apart from the TED
# figures rather than added to them — a contract above the thresholds is in
# both, and the two count differently (TED per notice, this per supplier and
# lot) — so each figure says where it is from.

UHM_ATTRIBUTION = "Uppgifter: Upphandlingsmyndigheten | Bearbetning: Agentanbud"
SME_SIZES = ("Mikroföretag", "Små företag", "Medelstora företag")
_KEEP_UPPER = {"AB", "HB", "KB", "AS", "ASA", "OY", "OYJ", "A/S", "APS", "GMBH", "AG", "SA",
               "BV", "NV", "LTD", "PLC", "LLC", "INC", "ABP", "IT", "VVS", "EL"}
_KEEP_LOWER = {"OCH", "I", "PÅ", "AV", "FÖR", "MED", "TILL", "&"}
_WORDS = {"LÄN", "VÄG", "RÖR", "SYD", "ÖST", "NYA", "VID", "BIL", "BYN", "ALL", "SKOG"}   # short, but words


def pretty_company(name: str) -> str:
    """A company name written in capitals, as the agency registers it, in
    ordinary case: "SVEVIA AB (PUBL)" -> "Svevia AB (publ)". Short words
    stay in capitals ("NCC", "WSP", "ÅF"): they are mostly initials, and
    "Ncc" would be wrong where "NCC" is at worst unchanged."""
    if not name or not name.isupper():
        return name or ""
    out = []
    for word in name.split():
        core = word.strip("()")
        if core in ("PUBL",):
            out.append(word.lower())
        elif core in _KEEP_UPPER or (len(core) <= 3 and core.isalpha()
                                     and core not in _KEEP_LOWER and core not in _WORDS):
            out.append(word)
        elif core in _KEEP_LOWER:
            out.append(word.lower())
        elif any(ch.isdigit() for ch in core):
            out.append(word)
        else:
            out.append("-".join(part[:1] + part[1:].lower() for part in word.split("-")))
    return " ".join(out)


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def aggregate_contracts(conn, authority: str = "", cpv: str = "", top: int = 15) -> dict:
    """Who was awarded contracts, from Upphandlingsmyndigheten's statistics.

    A win is a supplier on a lot: a framework agreement with five suppliers
    is five, as in aggregate_winners. Sole traders, whose names the agency
    anonymises, count in the totals but are never named.

    Returns {winners: [{name, orgnr, n}], contracts (supplier-lot rows),
    procurements, years: (first, last), sme_pct, below_threshold_pct,
    appealed_pct}; contracts is 0 when nothing matches or the table is empty.
    """
    import sqlite3
    where, params = ["1 = 1"], []
    if authority:
        # The agency writes "Göteborgs kommun" where the city says "Göteborgs Stad".
        a = authority.strip().casefold()
        a = a[:-4] + "kommun" if a.endswith(" stad") else a
        where.append("buyer_key LIKE ? ESCAPE '\\'")
        params.append(_like(a))
    if cpv:
        where.append("cpv LIKE ?")
        params.append("".join(ch for ch in cpv if ch.isdigit()) + "%")
    w = " AND ".join(where)
    empty = {"winners": [], "contracts": 0, "procurements": 0, "years": None,
             "sme_pct": None, "below_threshold_pct": None, "appealed_pct": None}
    try:
        tot = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT procurement_id), MIN(year), MAX(year), "
            "SUM(supplier_size IN (?, ?, ?)), SUM(supplier_size IS NOT NULL), "
            "COUNT(DISTINCT CASE WHEN above_threshold = 0 THEN procurement_id END), "
            "COUNT(DISTINCT CASE WHEN above_threshold IS NOT NULL THEN procurement_id END), "
            "COUNT(DISTINCT CASE WHEN appealed = 1 THEN procurement_id END), "
            "COUNT(DISTINCT CASE WHEN appealed IS NOT NULL THEN procurement_id END) "
            f"FROM contracts WHERE {w}", list(SME_SIZES) + params).fetchone()
    except sqlite3.OperationalError:      # table not created yet
        return empty
    if not tot or not tot[0]:
        return empty
    rows = conn.execute(
        "SELECT supplier_key, MAX(supplier_name), MAX(supplier_orgnr), COUNT(*) AS n FROM contracts "
        f"WHERE {w} AND supplier_name IS NOT NULL GROUP BY supplier_key ORDER BY n DESC, 2 LIMIT ?",
        params + [top]).fetchall()

    def pct(a, b):
        return round(100 * a / b) if b else None
    return {
        "winners": [{"name": pretty_company(r[1]), "orgnr": r[2], "n": r[3]} for r in rows],
        "contracts": tot[0], "procurements": tot[1], "years": (tot[2], tot[3]),
        "sme_pct": pct(tot[4] or 0, tot[5] or 0),
        "below_threshold_pct": pct(tot[6] or 0, tot[7] or 0),
        "appealed_pct": pct(tot[8] or 0, tot[9] or 0),
    }


def aggregate_winners(conn, authority: str = "", cpv: str = "") -> dict:
    """Count wins per supplier across award notices.

    winner_name holds a JSON list — framework agreements name several winners —
    so each row credits every supplier on it. Rows with an empty list are award
    notices with no named winner and are skipped, not counted as a supplier.

    Returns {wins: Counter, value_by_winner: dict, contracts: int}.
    """
    import json
    from collections import Counter, defaultdict

    where = ["source_system = 'ted_awards'", "winner_name IS NOT NULL", "winner_name != ''"]
    params: list = []
    if authority:
        where.append("authority LIKE ?")
        params.append(f"%{authority}%")
    if cpv:
        where.append("cpv_codes LIKE ?")
        params.append(f'%"{cpv}%')

    rows = conn.execute(
        f"SELECT winner_name, value FROM tenders WHERE {' AND '.join(where)} {AWARD_DEDUP_KEY}",
        params,
    ).fetchall()

    wins: Counter = Counter()
    value_by_winner: dict = {}
    seen: dict = defaultdict(Counter)   # kanonisk nyckel → stavningar
    contracts = 0
    for r in rows:
        try:
            winners = json.loads(r[0])
        except Exception:
            continue
        if not winners:
            continue
        contracts += 1
        # TED carries a placeholder value of 1 (or 0) SEK when the real award
        # value isn't published; treating it as real would report "8 wins — 8 SEK".
        val = r[1] if (r[1] and r[1] > 1) else None
        # A framework's value is the whole framework, not each supplier's take.
        # Crediting every named winner the full amount overstated IT awards by
        # 9.7x — 96.6bn reported against 9.98bn actually put out to tender,
        # because one contract names 99 winners and each was given all of it.
        # An equal split is an assumption: real call-offs are not even. But it
        # is bounded, and it makes the shares sum back to the money that exists,
        # which crediting in full never can. Callers say "estimated share".
        share = val / len(winners) if val else None
        for w in winners:
            key = canonical_supplier(w)
            if not key:
                continue
            # Report the spelling seen most often, so the label stays a real
            # name rather than the normalised key.
            seen[key][w] += 1
            wins[key] += 1
            if share:
                value_by_winner[key] = value_by_winner.get(key, 0.0) + float(share)
    label = {k: c.most_common(1)[0][0] for k, c in seen.items()}
    return {"wins": Counter({label[k]: v for k, v in wins.items()}),
            "value_by_winner": {label[k]: v for k, v in value_by_winner.items()},
            "contracts": contracts}


# How often a question can usefully be asked. Tenders arrive once a day, in
# one sync run starting at the cron hour; a search repeated before the next run
# gets the same answer. On 2026-10-16 one agent asked the same 177 searches
# every two minutes, ~28,000 times in a day. Telling agents when the data
# changes, in every answer, lets a well-built one poll once a day instead.

def data_freshness(conn) -> dict:
    """{'updated_at', 'next_update'} as ISO-8601 UTC strings (or None).

    updated_at is the last successful sync of a tender source; next_update is
    when the next sync run starts (it takes ~15 minutes to finish).
    """
    from datetime import datetime, timezone
    from app.cron import next_run

    placeholders = ",".join("?" * len(SOURCES))
    row = conn.execute(
        f"SELECT MAX(run_at) FROM sync_log WHERE status = 'ok' AND source IN ({placeholders})",
        SOURCES).fetchone()
    updated = None
    if row and row[0]:
        # sync_log.run_at is UTC, stored as 'YYYY-MM-DD HH:MM:SS'
        updated = str(row[0])[:19].replace(" ", "T") + "Z"
    nxt = next_run(datetime.now(timezone.utc))
    return {"updated_at": updated,
            "next_update": nxt.strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None}


def freshness_note(conn) -> str:
    """One line for the end of an MCP answer: when the data changes next."""
    f = data_freshness(conn)
    when = lambda iso: iso[:16].replace("T", " ") + " UTC"  # noqa: E731
    parts = []
    if f["updated_at"]:
        parts.append(f"senast hämtad {when(f['updated_at'])}")
    if f["next_update"]:
        parts.append(f"nästa hämtning startar {when(f['next_update'])}")
    detail = f" ({', '.join(parts)})" if parts else ""
    return (f"_Datan uppdateras en gång per dygn{detail}. Samma fråga före dess ger samma svar; "
            f"för bevakning räcker en fråga per dygn, efter klockan 07 UTC._")
