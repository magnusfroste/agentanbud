"""
Usage insights over `usage_log` — the numbers behind /analytics.

Kept in one place so every surface reports the same figures: the HTML page,
and the MCP `get_usage_stats` tool an operator agent uses to write about
traffic. Two copies of these rules would drift the moment one is tuned.

Everything here is aggregate and non-identifying — no IP addresses, no
user-agent strings, no visitor ids are ever stored.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional


SEGMENT_OTHER = "Övrigt"

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
    never the UA itself — so no fingerprinting, but sponsors still get an
    honest human-vs-automated traffic split."""
    ua = (user_agent or "").lower()
    if not ua:
        return True  # no UA at all → almost certainly automated
    return any(m in ua for m in _BOT_UA_MARKERS)




# "Real" searches only: rows flagged bot=0 (current logging) plus historical
# rows carrying an actual keyword. Excludes the old crawler filter-walks
# (query-less, unflagged) that once made this KPI ~99% bot noise.
REAL_SEARCH = ("action = 'search' AND (json_extract(meta, '$.bot') = 0 "
               "OR (query IS NOT NULL AND TRIM(query) != ''))")

# An operator agent polling get_usage_stats to write its daily report would
# otherwise top its own tool ranking and inflate the agent-call count it
# publishes. Counted separately as `operator_calls` rather than hidden.
SELF_REFERENTIAL_TOOLS = ("get_usage_stats",)

# Monitoring, not agents. These clients connect on a schedule regardless of
# whether anyone is using the site, so left in they would climb the client
# ranking and eventually top it — turning "which agents do people connect?"
# into a picture of our own infrastructure.
#   smoke        — scripts/smoke_test.py, a full handshake on every deploy
#   status-check — Easypanel's health check
# Counted separately as `monitoring_connects` rather than dropped, so the
# exclusion stays visible on the page and in the tool output.
SELF_REFERENTIAL_CLIENTS = ("smoke", "status-check")


def usage_summary(conn, days: Optional[int] = None, top: int = 15) -> dict:
    """Aggregate usage figures, optionally limited to the last `days` days.

    `days=None` means all time. Returns plain JSON-serialisable data with no
    presentation concerns (colours, bar widths) — callers decorate.
    """
    where, args, since = "", [], None
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        where, args = "WHERE created_at >= ?", [since]

    def _and(sql: str) -> str:
        return (where + " AND " + sql) if where else ("WHERE " + sql)

    def scalar(sql: str, extra: list | None = None) -> int:
        return conn.execute(f"SELECT COUNT(*) FROM usage_log {_and(sql)}",
                            args + (extra or [])).fetchone()[0] or 0

    self_names = [f"tool:{n}" for n in SELF_REFERENTIAL_TOOLS]
    not_self = " AND action NOT IN (%s)" % ",".join("?" * len(self_names))

    # The smoke test does a full handshake AND several tool calls on every
    # deploy. Filtering only its handshake would leave its calls inflating
    # agent_calls, so exclude its whole session: the connect event carries
    # both the client name and the session hash, which links the two.
    self_clients = list(SELF_REFERENTIAL_CLIENTS)
    _mon_sids = ("SELECT json_extract(meta, '$._sid') FROM usage_log "
                 "WHERE action = 'mcp:connect' AND json_extract(meta, '$.client') IN (%s)"
                 % ",".join("?" * len(self_clients)))
    not_monitoring = (" AND COALESCE(json_extract(meta, '$._sid'), '') NOT IN (%s)" % _mon_sids)

    def rows_(c, sql, params):
        return c.execute(sql, params).fetchall()

    human_views = scalar("action = 'view' AND json_extract(meta, '$.bot') = 0")
    # Bot views live in the aggregated daily counter; legacy per-hit rows are
    # still counted until they age out of usage_log.
    counter_where, counter_args = "WHERE kind = 'bot_view'", []
    if days:
        counter_where += " AND day >= DATE('now', ?)"
        counter_args = [f"-{days} days"]
    bot_views = (conn.execute(
        f"SELECT COALESCE(SUM(n), 0) FROM daily_counters {counter_where}", counter_args
    ).fetchone()[0] or 0) + scalar("action = 'view' AND json_extract(meta, '$.bot') = 1")
    view_total = human_views + bot_views

    searches = scalar(REAL_SEARCH)
    agent_calls = scalar("action LIKE 'tool:%'" + not_self + not_monitoring,
                         self_names + self_clients)
    operator_calls = scalar("action IN (%s)" % ",".join("?" * len(self_names)), self_names)
    # Excludes the operator's own polling for the same reason agent_calls does —
    # otherwise the reporter counts itself as one of the "unique agents".
    agent_sessions = conn.execute(
        "SELECT COUNT(DISTINCT json_extract(meta, '$._sid')) FROM usage_log "
        + _and("action LIKE 'tool:%' AND json_extract(meta, '$._sid') IS NOT NULL")
        + not_self + not_monitoring,
        args + self_names + self_clients,
    ).fetchone()[0] or 0
    api_calls = scalar("channel = 'api'")

    # --- MCP adoption: who is connecting, not just how much ---------------
    # Every MCP client self-identifies in the initialize handshake. Counting
    # distinct sessions per client name answers "which agents are people
    # actually wiring up?" — the question the raw call count cannot.
    not_self_client = (" AND COALESCE(json_extract(meta, '$.client'), '') NOT IN (%s)"
                       % ",".join("?" * len(self_clients)))
    connects = scalar("action = 'mcp:connect'" + not_self_client, self_clients)
    monitoring_connects = scalar(
        "action = 'mcp:connect' AND json_extract(meta, '$.client') IN (%s)"
        % ",".join("?" * len(self_clients)), self_clients)
    client_rows = rows_(
        conn,
        "SELECT json_extract(meta, '$.client') AS client, "
        "COUNT(*) AS handshakes, "
        "COUNT(DISTINCT json_extract(meta, '$._sid')) AS sessions "
        "FROM usage_log " + _and("action = 'mcp:connect'") + not_self_client
        + " GROUP BY client ORDER BY sessions DESC, handshakes DESC LIMIT ?",
        args + self_clients + [top],
    )
    events_total = scalar("1 = 1")

    # --- industry demand ------------------------------------------------
    demand_rows = conn.execute(
        "SELECT query, meta FROM usage_log "
        + _and("(action = 'search' OR action LIKE 'tool:%')"
               " AND query IS NOT NULL AND TRIM(query) != ''"),
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

    def rows(sql: str, extra: list | None = None):
        return conn.execute(sql, args + (extra or [])).fetchall()

    term_rows = rows(
        "SELECT LOWER(TRIM(query)) AS term, COUNT(*) AS n FROM usage_log "
        + _and("query IS NOT NULL AND TRIM(query) != '' AND action != 'view'")
        + " GROUP BY term ORDER BY n DESC LIMIT ?", [top])

    gap_rows = rows(
        "SELECT LOWER(TRIM(query)) AS term, COUNT(*) AS n FROM usage_log "
        + _and("action = 'search' AND json_extract(meta, '$.results') = 0"
               " AND query IS NOT NULL AND TRIM(query) != ''")
        + " GROUP BY term ORDER BY n DESC LIMIT ?", [top])

    tool_rows = rows(
        "SELECT action, COUNT(*) AS n FROM usage_log "
        + _and("action LIKE 'tool:%'") + not_self + not_monitoring
        + " GROUP BY action ORDER BY n DESC LIMIT ?", self_names + self_clients + [top])

    # --- 30-day series: human pageviews only, so growth means real interest
    day_rows = conn.execute(
        "SELECT DATE(created_at) AS day, "
        "SUM(action = 'view' AND json_extract(meta, '$.bot') = 0) AS views, "
        f"SUM({REAL_SEARCH}) AS searches, "
        f"SUM(action LIKE 'tool:%'{not_self}) AS agents "
        "FROM usage_log WHERE created_at >= DATE('now', '-29 days') GROUP BY day",
        self_names,
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
        "period_days": days, "since": since,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events_total": events_total,
        "visits": {"total": view_total, "human": human_views, "bot": bot_views,
                   "human_pct": int(human_views / view_total * 100) if view_total else 0},
        "searches": searches,
        "agent_calls": agent_calls,
        "agent_sessions": agent_sessions,
        "operator_calls": operator_calls,
        "api_calls": api_calls,
        "segments": [{"label": l, "n": n, "pct": int(n / seg_total * 100)}
                     for l, n in seg_counter.most_common()],
        "top_terms": [{"term": r["term"], "n": r["n"]} for r in term_rows],
        "unmet_demand": [{"term": r["term"], "n": r["n"]} for r in gap_rows],
        "unclassified": [{"term": t, "n": n} for t, n in other_counter.most_common(top)],
        "other_share_pct": int(seg_counter[SEGMENT_OTHER] / seg_total * 100),
        "mcp_tools": [{"tool": r["action"][5:], "n": r["n"]} for r in tool_rows],
        "mcp_connects": connects,
        "monitoring_connects": monitoring_connects,
        "mcp_clients": [
            {"client": r["client"] or "okänd", "sessions": r["sessions"],
             "handshakes": r["handshakes"]}
            for r in client_rows
        ],
        "daily_last_30": daily,
    }


def format_usage_markdown(s: dict) -> str:
    """Render a summary as markdown — what an operator agent wants when it is
    about to write a post about the numbers."""
    period = f"senaste {s['period_days']} dygnen" if s["period_days"] else "hela perioden"
    v = s["visits"]
    out = [f"# Agentanbud — användning ({period})", "",
           f"**Besök:** {v['total']} ({v['human']} mänskliga / {v['bot']} botar & crawlers, "
           f"{v['human_pct']}% mänskliga)",
           f"**Sökningar på webben:** {s['searches']}",
           f"**MCP-anslutningar:** {s.get('mcp_connects', 0)}"
        + (f" (exkl. {s['monitoring_connects']} från vår egen driftövervakning)"
           if s.get("monitoring_connects") else "")
        + (f" från {len(s.get('mcp_clients') or [])} olika klienter" if s.get("mcp_clients") else ""),
        f"**MCP-agentanrop:** {s['agent_calls']}"
           + (f" från {s['agent_sessions']} unika agentsessioner" if s["agent_sessions"] else "")
           + (f" (exkl. {s['operator_calls']} egna get_usage_stats-anrop)"
              if s["operator_calls"] else ""), ""]
    if s["segments"]:
        out.append("## Efterfrågan per bransch")
        out += [f"- {x['label']}: {x['n']} sökningar ({x['pct']}%)" for x in s["segments"][:10]]
        out.append("")
    if s["top_terms"]:
        out.append("## Mest sökta termer")
        out += [f"- {x['term']}: {x['n']}" for x in s["top_terms"][:10]]
        out.append("")
    if s["unmet_demand"]:
        out.append("## Sökningar utan träff (luckor i datan)")
        out += [f"- {x['term']}: {x['n']}" for x in s["unmet_demand"][:10]]
        out.append("")
    if s.get("mcp_clients"):
        out.append("## Vilka AI-agenter ansluter")
        out += [f"- {c['client']}: {c['sessions']} sessioner ({c['handshakes']} anslutningar)"
                for c in s["mcp_clients"][:10]]
        out.append("")
    if s["mcp_tools"]:
        out.append("## Mest använda MCP-verktyg")
        out += [f"- {x['tool']}: {x['n']}" for x in s["mcp_tools"][:8]]
        out.append("")
    recent = s["daily_last_30"][-7:]
    if recent:
        out.append("## Senaste 7 dygnen (besök / sök / agent)")
        out += [f"- {d['date']}: {d['views']} / {d['searches']} / {d['agent_calls']}" for d in recent]
        out.append("")
    out.append("Siffrorna är aggregerade och innehåller inga personuppgifter — inga "
               "cookies, ingen IP-adress, inget besökar-ID. Fria att publicera och citera.")
    return "\n".join(out)
