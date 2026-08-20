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
SOURCES = ["mercell", "ted", "ted_awards", "ted_pin", "lov"]

SOURCE_DESCRIPTION = (
    "Data source filter. 'mercell' = most Swedish tenders, 'ted' = EU-threshold "
    "notices, 'ted_awards' = awarded contracts, 'ted_pin' = planned procurements, "
    "'lov' = LOV services."
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
