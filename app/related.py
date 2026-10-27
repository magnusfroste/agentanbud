"""
What a tender page adds to the notice: CPV codes in words, similar open
tenders, and what we know about the buyer.

A tender page used to show what the source shows — title, dates, description,
bare CPV numbers — so to a reader and to a search engine it was a copy of the
notice. The MCP tools already answered the questions a supplier asks next
(similar_tenders, get_authority, get_winner_history); this brings the same
answers to the page, from the same data.

CPV labels are the EU's own Swedish names (CPV 2008, 9,454 codes), fetched
from the Publications Office (data.europa.eu/cpv) into app/vocab/cpv_sv.json (not
"data/", which git and the Docker build ignore: that is where the database
lives).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app import seo

CPV_FILE = Path(__file__).parent / "vocab" / "cpv_sv.json"


@lru_cache(maxsize=1)
def _labels() -> dict:
    try:
        return json.loads(CPV_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cpv_label(code: str) -> Optional[str]:
    """The Swedish name of a CPV code, or of its nearest parent.

    Sources drop the check digit and sometimes use codes finer than the
    vocabulary; zeroing digits from the right walks up the tree
    (45331100 → 45331000 → 45330000 …) until a named code is found.
    """
    code = "".join(ch for ch in str(code or "") if ch.isdigit())[:8].ljust(8, "0")
    if len(code) != 8 or code == "00000000":
        return None
    labels = _labels()
    for keep in range(8, 1, -1):
        candidate = code[:keep].ljust(8, "0")
        if candidate in labels:
            return labels[candidate]
    return None


def cpv_list(codes) -> list[dict]:
    return [{"code": str(c), "label": cpv_label(c)} for c in (codes or [])]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_OPEN = "source_system != 'ted_awards' AND deadline IS NOT NULL AND deadline > ?"


def similar_open(conn, t: dict, limit: int = 5) -> list[dict]:
    """Open tenders sharing a CPV group (first four digits) or the buyer.

    Scored as the MCP tool similar_tenders does: two points per shared CPV
    code, three for the same buyer; soonest deadline breaks ties.
    """
    codes = [str(c) for c in (t.get("cpv_codes_list") or [])]
    buyer = (t.get("authority") or "").strip()
    clauses, args = [], []
    for p in sorted({c[:4] for c in codes if len(c) >= 4}):
        clauses.append("cpv_codes LIKE ?")
        args.append(f'%"{p}%')
    if buyer:
        clauses.append("authority = ?")
        args.append(buyer)
    if not clauses:
        return []
    rows = conn.execute(
        f"SELECT id, title, authority, deadline, region, cpv_codes FROM tenders "
        f"WHERE ({' OR '.join(clauses)}) AND id != ? AND {_OPEN} LIMIT 400",
        args + [t.get("id") or -1, _now_iso()]).fetchall()
    mine = set(codes)
    scored = []
    for r in rows:
        try:
            theirs = set(json.loads(r["cpv_codes"] or "[]"))
        except ValueError:
            theirs = set()
        score = 2 * len(mine & {str(c) for c in theirs}) + (3 if buyer and r["authority"] == buyer else 0)
        if score:
            scored.append((-score, r["deadline"] or "", r))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [{"id": r["id"], "title": r["title"], "authority": r["authority"], "region": r["region"],
             "deadline_text": seo._swedish_date(r["deadline"])} for _, _, r in scored[:limit]]


_SPELLINGS: dict = {}


def _spellings(conn) -> dict:
    """lower-cased buyer name (without town) → every spelling in the table.

    A few thousand distinct names, read off the authority index and kept
    until the table grows or shrinks. Matching in Python, then asking for
    the exact spellings with IN, lets SQLite use that index; comparing every
    row case-insensitively cost 85 ms a page.
    """
    size = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    hit = _SPELLINGS.get("v")
    if hit and hit[0] == size:
        return hit[1]
    by_name: dict = {}
    for (a,) in conn.execute("SELECT DISTINCT authority FROM tenders WHERE authority IS NOT NULL"):
        by_name.setdefault(seo._buyer(a).lower(), []).append(a)
    _SPELLINGS["v"] = (size, by_name)
    return by_name


def buyer_match(conn, full: str, name: str) -> tuple[str, list]:
    """SQL matching one buyer, any case: "Region Halland", "REGION HALLAND"
    and "Region Halland, HALMSTAD" are one buyer. (SQLite folds case for A–Z
    only, so Å, Ä and Ö are compared in Python.)"""
    spellings = _spellings(conn).get(seo._buyer(name).lower(), []) or [full]
    return f"authority IN ({','.join('?' * len(spellings))})", list(spellings)


def buyer_profile(conn, authority: Optional[str]) -> Optional[dict]:
    """Open notices, usual industries and usual winners for this buyer.

    The buyer is matched on its name as the sources write it, with or without
    the ", TOWN" Mercell and TED append — not on a substring, which for a
    name like "Region" would sweep in every region in the country.
    """
    full = (authority or "").strip()
    name = seo._buyer(full)
    if len(name) < 4:
        return None
    match, margs = buyer_match(conn, full, name)
    open_n = conn.execute(f"SELECT COUNT(*) FROM tenders WHERE {match} AND {_OPEN}",
                          margs + [_now_iso()]).fetchone()[0]
    total = conn.execute(f"SELECT COUNT(*) FROM tenders WHERE {match} AND source_system != 'ted_awards'",
                         margs).fetchone()[0]
    divisions: Counter = Counter()
    for (raw,) in conn.execute(f"SELECT cpv_codes FROM tenders WHERE {match}", margs):
        try:
            for d in {str(c)[:2] for c in json.loads(raw or "[]") if c}:
                divisions[d] += 1
        except ValueError:
            continue
    usual = [{"code": d, "label": cpv_label(d + "000000") or f"CPV {d}", "n": n}
             for d, n in divisions.most_common(3)]
    from mcp_shared import aggregate_winners
    agg = aggregate_winners(conn, buyer=(full, name))
    winners = [{"name": w, "n": n} for w, n in agg["wins"].most_common(3)]
    return {"name": name, "open": open_n, "notices": total, "usual": usual,
            "winners": winners, "contracts": agg["contracts"]}
