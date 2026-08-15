"""
Usage insights over `usage_log` — the numbers behind /analytics.

Kept separate from app/main.py so every surface reads the same figures:
the HTML page, the REST endpoint (/api/analytics) and the MCP tool
(get_usage_stats). An operator agent reporting on traffic must not have to
scrape the HTML page to get them.

Everything here is aggregate and non-identifying: we never store IP
addresses, user-agent strings or visitor ids, so there is nothing
per-person to expose.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

# ---- Sponsor-facing demand segments -----------------------------------------
# Buckets a free-text query / CPV prefix into a broad industry segment. This is
# what turns raw search logs into "your industry gets N searches/month" — the
# number a sponsor actually cares about. Coarse on purpose: one keyword hit is
# enough, first match wins.
SEGMENTS: list[tuple[str, list[str], list[str]]] = [
    # (label, keyword substrings, CPV 2-digit prefixes)
    ("IT & Digitalisering", ["it-", "it ", "digital", "system", "mjukvar", "programvar",
                             "webb", "app", "moln", "cloud", "data", "cyber", "e-tjänst",
                             "licens", "server", "nätverk"], ["48", "72", "30", "32"]),
    ("Bygg & Anläggning", ["bygg", "anläggning", "väg", "entreprenad", "ombyggnad",
                           "nybyggnad", "renover", "mark", "betong", "asfalt"], ["45", "44", "71"]),
    ("Vård & Omsorg", ["vård", "omsorg", "sjuk", "hälsa", "medicin", "läkemedel",
                       "bemanning", "äldre", "hemtjänst", "tandvård", "assistans"], ["85", "33"]),
    ("Transport & Logistik", ["transport", "logistik", "fordon", "buss", "taxi",
                              "frakt", "gods", "färdtjänst", "skolskjuts"], ["60", "34", "63"]),
    ("Livsmedel & Måltid", ["livsmedel", "mat", "måltid", "kost", "catering",
                            "skolmat", "dryck"], ["15", "55"]),
    ("Utbildning", ["utbildning", "skola", "förskola", "lärande", "kurs",
                    "pedagog", "läromedel"], ["80"]),
    ("Städ & Fastighetsservice", ["städ", "lokalvård", "fastighetsservice",
                                  "förvaltning", "fastighetsskötsel"], ["90", "70", "77"]),
    ("Energi & Miljö", ["energi", "elförsörjning", "miljö", "sanering", "avfall",
                        "återvinning", "solcell", "värme", "vatten", "va-"], ["09", "31", "65", "41"]),
    ("Juridik & Konsult", ["juridik", "juridisk", "advokat", "rådgivning", "revision",
                           "konsult", "utredning", "upphandlingsstöd"], ["79", "66", "75"]),
    ("Möbler & Inredning", ["möbel", "inredning", "kontorsmöbl", "belysning"], ["39"]),
    ("Säkerhet & Bevakning", ["säkerhet", "bevakning", "larm", "väktare",
                              "brandskydd"], ["35"]),
]
SEGMENT_OTHER = "Övrigt"

# Tools that only read the usage stats themselves. An operator agent polling
# get_usage_stats to write its daily report would otherwise dominate the tool
# ranking and inflate the agent-call count it then publishes — it'd be
# reporting mostly on its own polling. Counted separately as `operator_calls`
# so the figure is visible rather than silently dropped.
SELF_REFERENTIAL_TOOLS = ("get_usage_stats",)


def segment_for(query: Optional[str], cpv: Optional[str]) -> str:
    """Map a search to a sponsor-relevant industry segment. First match wins."""
    text = (query or "").lower()
    cpv2 = (cpv or "")[:2]
    for label, keywords, cpv_prefixes in SEGMENTS:
        if cpv2 and cpv2 in cpv_prefixes:
            return label
        if text and any(kw in text for kw in keywords):
            return label
    return SEGMENT_OTHER


_BOT_UA_MARKERS = ("bot", "crawler", "spider", "slurp", "bingpreview", "facebookexternalhit",
                   "gptbot", "claudebot", "ccbot", "perplexity", "python-requests",
                   "curl", "wget", "headless", "scrapy", "httpx", "go-http", "java/")


def looks_like_bot(user_agent: str) -> bool:
    """Coarse bot detection from the UA string. We store only this boolean —
    never the UA itself — so no fingerprinting, but reporting still gets an
    honest human-vs-automated traffic split."""
    ua = (user_agent or "").lower()
    if not ua:
        return True  # no UA at all → almost certainly automated
    return any(m in ua for m in _BOT_UA_MARKERS)


def usage_summary(conn, days: Optional[int] = None, top: int = 15) -> dict:
    """Aggregate usage figures, optionally limited to the last `days` days.

    `days=None` means all time. Returns plain JSON-serialisable data — no
    presentation concerns (colours, bar widths) live here.
    """
    where, args = "", []
    since = None
    if days:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
        since = since_dt.strftime("%Y-%m-%d %H:%M:%S")
        where, args = "WHERE created_at >= ?", [since]

    def scalar(sql: str, extra: list | None = None) -> int:
        clause = where + (" AND " + sql if where else "WHERE " + sql) if sql else where
        return conn.execute(
            f"SELECT COUNT(*) FROM usage_log {clause}", args + (extra or [])
        ).fetchone()[0]

    # SQL fragment + params excluding the self-referential tools, reused by the
    # agent-call count and the tool ranking so they always agree.
    _self_names = [f"tool:{n}" for n in SELF_REFERENTIAL_TOOLS]
    _not_self = " AND action NOT IN (%s)" % ",".join("?" * len(_self_names))

    events_total = scalar("")
    views = scalar("action = 'view'")
    human_views = scalar("action = 'view' AND json_extract(meta, '$.bot') = 0")
    searches = scalar("action = 'search'")
    agent_calls = scalar("action LIKE 'tool:%'" + _not_self, _self_names)
    operator_calls = scalar(
        "action IN (%s)" % ",".join("?" * len(_self_names)), _self_names)

    # --- industry demand: classify every query, all channels -------------
    demand_rows = conn.execute(
        f"SELECT query, meta FROM usage_log {where}"
        + (" AND" if where else " WHERE")
        + " (action = 'search' OR action LIKE 'tool:%')"
        " AND query IS NOT NULL AND TRIM(query) != ''",
        args,
    ).fetchall()
    seg_counter: Counter = Counter()
    other_counter: Counter = Counter()
    for r in demand_rows:
        cpv = None
        if r["meta"]:
            try:
                cpv = json.loads(r["meta"]).get("cpv") or None
            except Exception:
                pass
        seg = segment_for(r["query"], cpv)
        seg_counter[seg] += 1
        if seg == SEGMENT_OTHER:
            other_counter[r["query"].strip().lower()] += 1
    seg_total = sum(seg_counter.values()) or 1

    def rows(sql: str, extra: list | None = None) -> list:
        return conn.execute(sql, args + (extra or [])).fetchall()

    term_rows = rows(
        f"SELECT LOWER(TRIM(query)) AS term, COUNT(*) AS n FROM usage_log {where}"
        + (" AND" if where else " WHERE")
        + " query IS NOT NULL AND TRIM(query) != '' AND action != 'view'"
        " GROUP BY term ORDER BY n DESC LIMIT ?", [top])

    gap_rows = rows(
        f"SELECT LOWER(TRIM(query)) AS term, COUNT(*) AS n FROM usage_log {where}"
        + (" AND" if where else " WHERE")
        + " action = 'search' AND json_extract(meta, '$.results') = 0"
        " AND query IS NOT NULL AND TRIM(query) != ''"
        " GROUP BY term ORDER BY n DESC LIMIT ?", [top])

    tool_rows = rows(
        f"SELECT action, COUNT(*) AS n FROM usage_log {where}"
        + (" AND" if where else " WHERE")
        + " action LIKE 'tool:%'" + _not_self
        + " GROUP BY action ORDER BY n DESC LIMIT ?", _self_names + [top])

    # --- daily series (always last 30 days, regardless of `days`) --------
    day_rows = conn.execute(
        "SELECT DATE(created_at) AS day,"
        " SUM(action = 'view') AS views,"
        " SUM(action = 'search') AS searches,"
        " SUM(action LIKE 'tool:%'" + _not_self + ") AS agents"
        " FROM usage_log WHERE created_at >= DATE('now', '-29 days') GROUP BY day",
        _self_names,
    ).fetchall()
    by_day = {r["day"]: r for r in day_rows}
    today = datetime.now(timezone.utc).date()
    daily = []
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        r = by_day.get(d.isoformat())
        v = (r["views"] if r else 0) or 0
        s = (r["searches"] if r else 0) or 0
        a = (r["agents"] if r else 0) or 0
        daily.append({"date": d.isoformat(), "views": v, "searches": s,
                      "agent_calls": a, "total": v + s + a})

    return {
        "period_days": days,
        "since": since,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events_total": events_total,
        "visits": {
            "total": views,
            "human": human_views,
            "bot": views - human_views,
            "human_pct": int(human_views / views * 100) if views else 0,
        },
        "searches": searches,
        # Excludes the operator's own get_usage_stats polling — see
        # SELF_REFERENTIAL_TOOLS. That count is reported separately.
        "agent_calls": agent_calls,
        "operator_calls": operator_calls,
        "segments": [
            {"label": label, "n": n, "pct": int(n / seg_total * 100)}
            for label, n in seg_counter.most_common()
        ],
        "top_terms": [{"term": r["term"], "n": r["n"]} for r in term_rows],
        "unmet_demand": [{"term": r["term"], "n": r["n"]} for r in gap_rows],
        "unclassified": [{"term": t, "n": n} for t, n in other_counter.most_common(top)],
        "other_share_pct": int(seg_counter[SEGMENT_OTHER] / seg_total * 100),
        "mcp_tools": [{"tool": r["action"][5:], "n": r["n"]} for r in tool_rows],
        "daily_last_30": daily,
    }


def format_usage_markdown(s: dict) -> str:
    """Render a usage summary as markdown — the shape an LLM operator agent
    wants when it's about to write a post about the numbers."""
    period = f"senaste {s['period_days']} dygnen" if s["period_days"] else "hela perioden"
    v = s["visits"]
    out = [
        f"# Agentanbud — användning ({period})",
        "",
        f"**Besök:** {v['total']} ({v['human']} mänskliga / {v['bot']} botar & agenter, "
        f"{v['human_pct']}% mänskliga)",
        f"**Sökningar på webben:** {s['searches']}",
        f"**MCP-agentanrop:** {s['agent_calls']}"
        + (f" (exkl. {s['operator_calls']} egna get_usage_stats-anrop)"
           if s.get("operator_calls") else ""),
        f"**Händelser totalt:** {s['events_total']}",
        "",
    ]
    if s["segments"]:
        out.append("## Efterfrågan per bransch")
        for seg in s["segments"][:10]:
            out.append(f"- {seg['label']}: {seg['n']} sökningar ({seg['pct']}%)")
        out.append("")
    if s["top_terms"]:
        out.append("## Mest sökta termer")
        for t in s["top_terms"][:10]:
            out.append(f"- {t['term']}: {t['n']}")
        out.append("")
    if s["unmet_demand"]:
        out.append("## Sökningar utan träff (luckor i datan)")
        for t in s["unmet_demand"][:10]:
            out.append(f"- {t['term']}: {t['n']}")
        out.append("")
    if s["mcp_tools"]:
        out.append("## Mest använda MCP-verktyg")
        for t in s["mcp_tools"][:8]:
            out.append(f"- {t['tool']}: {t['n']}")
        out.append("")
    recent = s["daily_last_30"][-7:]
    if recent:
        out.append("## Senaste 7 dygnen (besök / sök / agent)")
        for d in recent:
            out.append(f"- {d['date']}: {d['views']} / {d['searches']} / {d['agent_calls']}")
        out.append("")
    out.append(
        "Datan är aggregerad och innehåller inga personuppgifter — inga cookies, "
        "ingen IP-adress, inget besökar-ID. Fri att publicera och citera."
    )
    return "\n".join(out)
