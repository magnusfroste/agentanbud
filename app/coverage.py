"""
How much of Swedish public procurement the live sources see.

Every notice in Sweden is published in one of five registered advertising
databases, and every notice above the EU thresholds also on TED. The live
sources here are TED (all directive-governed notices), Mercell (which now
includes the former Visma TendSign and Opic) and KommersAnnons. What they
cannot see is below the thresholds in the other databases: e-Avrop, whose
robots.txt forbids automated reading, Clira and Konstpool.

Upphandlingsmyndigheten's yearly notice statistics (uhm_notices) list every
notice with its database and whether it was directive-governed, so the share
is measured rather than guessed: in 2025, 2 101 of 18 015 notices (12 %).

This counts what the sources publish, not what the scrapers caught on a given
day — a page missed by the Mercell walk is not in it.
"""
from __future__ import annotations

from typing import Optional

# Databases read live (scraper/mercell.py, scraper/kommersannons.py).
READ_LIVE = ("Mercell Annonsdatabas", "KommersAnnons.se")

ATTRIBUTION = ("Uppgifter: Upphandlingsmyndigheten, Antal upphandlingar | "
               "Bearbetning: Agentanbud")


def coverage(conn, year: Optional[int] = None) -> Optional[dict]:
    """The latest year in uhm_notices (or `year`): total notices, how many the
    live sources reach, and per database. None when the table is empty."""
    try:
        if year is None:
            year = conn.execute("SELECT MAX(year) FROM uhm_notices").fetchone()[0]
        if year is None:
            return None
        rows = conn.execute(
            "SELECT ad_database, COUNT(*) AS n, SUM(above_threshold = 1) AS above "
            "FROM uhm_notices WHERE year = ? GROUP BY ad_database ORDER BY n DESC", (year,)
        ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    total = sum(r["n"] for r in rows)
    databases, missed = [], 0
    for r in rows:
        name, n, above = r["ad_database"] or "okänd", r["n"], r["above"] or 0
        live = name in READ_LIVE
        # Directive-governed notices are on TED whatever the database; the
        # rest (below the thresholds, or not stated) only where they were put.
        unseen = 0 if live else n - above
        missed += unseen
        databases.append({"database": name, "notices": n, "above_threshold": above,
                          "read_live": live, "unseen": unseen})
    return {"year": year, "notices": total, "reached": total - missed, "unseen": missed,
            "reached_pct": round(100 * (total - missed) / total, 1),
            "databases": databases, "attribution": ATTRIBUTION}


def summary_lines(cov: Optional[dict]) -> list[str]:
    """Markdown lines for get_stats and the like; empty without data."""
    if not cov:
        return []
    lines = [f"**Täckning {cov['year']}:** de löpande källorna når {cov['reached']} av "
             f"{cov['notices']} annonserade upphandlingar i Sverige ({cov['reached_pct']} %)."]
    gaps = [d for d in cov["databases"] if d["unseen"]]
    if gaps:
        lines.append("Utom räckhåll, under EU:s tröskelvärden: "
                     + ", ".join(f"{d['database']} {d['unseen']}" for d in gaps)
                     + ". Över tröskelvärdena finns allt i TED.")
    lines.append(f"_{cov['attribution']}_")
    return lines


def buyer_history(conn, name: str) -> Optional[dict]:
    """A buyer's notices per year in every database, from uhm_notices:
    {buyer, years: [{year, notices, unseen}]}, where unseen are those the live
    sources miss. The buyer is found the way buyer pages match names
    (app/related.buyer_match_key: "Göteborgs Stad" is GÖTEBORGS KOMMUN); when
    several match, the one with the most notices. None without a match."""
    from app.related import buyer_match_key
    key = buyer_match_key(name or "")
    if len(key) < 4:
        return None
    marks = ",".join("?" * len(READ_LIVE))
    try:
        top = conn.execute(
            "SELECT buyer_name, buyer_key, COUNT(*) AS n FROM uhm_notices WHERE buyer_key LIKE ? "
            "GROUP BY buyer_key ORDER BY n DESC LIMIT 1", (f"%{key}%",)).fetchone()
        if not top:
            return None
        rows = conn.execute(
            f"SELECT year, COUNT(*) AS n, "
            f"SUM(COALESCE(ad_database, '') NOT IN ({marks}) AND COALESCE(above_threshold, 0) = 0) AS unseen "
            f"FROM uhm_notices WHERE buyer_key = ? GROUP BY year ORDER BY year",
            (*READ_LIVE, top["buyer_key"])).fetchall()
    except Exception:
        return None
    return {"buyer": top["buyer_name"],
            "years": [{"year": r["year"], "notices": r["n"], "unseen": r["unseen"] or 0} for r in rows]}


def history_lines(hist: Optional[dict]) -> list[str]:
    """Markdown lines for get_authority; empty without data."""
    if not hist or not hist["years"]:
        return []
    per_year = ", ".join(f"{y['year']}: {y['notices']}" for y in hist["years"])
    unseen = sum(y["unseen"] for y in hist["years"])
    lines = [f"**Annonserade upphandlingar i alla annonsdatabaser ({hist['buyer']}):** {per_year}."]
    if unseen:
        lines.append(f"Varav {unseen} under tröskelvärdena i databaser som inte läses löpande här "
                     f"(främst e-Avrop) — de syns inte bland de öppna upphandlingarna.")
    lines.append(f"_{ATTRIBUTION}_")
    return lines
