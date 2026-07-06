"""
MCP-server för Agentanbud — stdio-transport.

Wraps Agentanbud's REST API as MCP tools so Claude Code / Kilo Code / Cline /
any MCP-kompatibel klient kan söka och läsa svenska upphandlingar.

Följer best practices för LLM-tool-design:
- Korta descriptions (1 mening + exempel)
- Tydliga enums (inga hallucinerade parametrar)
- Säkra defaults (open_only=true)
- Markdown-formaterad output (LLM-vänlig)
- Paywall/auth info per källa (agenten vet om konto krävs)

Användning:
  python -m mcp_server

Claude Desktop config:
  {"mcpServers": {"agentanbud": {"command": "python", "args": ["-m", "mcp_server"]}}}
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

# Reuse Agentanbud's DB layer
sys.path.insert(0, str(Path(__file__).parent))
from app.db import connect  # noqa: E402

DB_PATH = os.environ.get("DB_PATH", "/data/application.db")

server = Server("agentanbud")


# ----- Provider metadata (paywall/auth info) -------------------------------

PROVIDERS = {
    "mercell": {
        "name": "Mercell",
        "url": "https://search-service-api.discover.app.mercell.com/",
        "auth": "open",  # data is open, but to APPLY you need an account
        "data_status": "live",
    },
    "ted": {
        "name": "TED EU",
        "url": "https://ted.europa.eu/",
        "auth": "open",  # fully public, no account needed
        "data_status": "live",
    },
    "ted_pin": {
        "name": "TED EU PIN",
        "url": "https://api.ted.europa.eu/v3/notices/search",
        "auth": "open",
        "data_status": "live",
        "note": "Prior Information Notices — upphandlingar som planeras, innan formell annons.",
    },
    "lov": {
        "name": "Upphandlingsmyndigheten LOV",
        "url": "https://www.upphandlingsmyndigheten.se/hitta-lov-uppdrag/",
        "auth": "open",  # att ansöka kräver konto hos kommunen, men data är publikt
        "data_status": "live",
        "note": "Valfrihetssystem — hemtjänst, äldreboende, personlig assistans etc. Inga deadlines, löpande ansökan.",
    },
    "criteria": {
        "name": "Upphandlingsmyndigheten Hållbarhetskriterier",
        "url": "https://www.upphandlingsmyndigheten.se/kriterier/",
        "auth": "open",
        "data_status": "live",
        "note": "Hållbarhetskrav per bransch — IT, transport, livsmedel, bygg etc. Använd som referens vid anbudsskrivning.",
        "type": "knowledge",
    },
    "questions": {
        "name": "Upphandlingsmyndigheten Frågeportalen",
        "url": "https://www.upphandlingsmyndigheten.se/fragor-och-svar/",
        "auth": "open",
        "data_status": "live",
        "note": "Q&A om LOU, LOV, tröskelvärden, direktupphandling etc. från Upphandlingsmyndighetens jurister.",
        "type": "knowledge",
    },
    "tendsign": {
        "name": "Tendsign (Visma)",
        "url": "https://tendsign.com/",
        "auth": "required",  # to apply
        "data_status": "not_implemented",
    },
    "eavrop": {
        "name": "e-Avrop",
        "url": "https://www.e-avrop.com/",
        "auth": "required",
        "data_status": "not_implemented",
    },
    "kommersannons": {
        "name": "Kommersannons",
        "url": "https://www.kommersannons.se/",
        "auth": "required",
        "data_status": "not_implemented",
    },
    "clira": {
        "name": "Clira (Esource)",
        "url": "https://esource.clira.io/",
        "auth": "required",  # plus it's a paid SaaS
        "data_status": "not_implemented",
    },
}


# ----- Helpers ---------------------------------------------------------------

def _row_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("cpv_codes", "raw_json"):
        if d.get(k) and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def _days_until(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str[:19])
        return (dt - datetime.now()).days
    except Exception:
        return None


def _format_tender(t: dict) -> str:
    """Format a tender as readable markdown for the LLM."""
    src = t.get("source_system", "")
    provider = PROVIDERS.get(src, {})

    lines = [f"**{t.get('title') or '(utan titel)'}**"]
    lines.append(f"Upphandlare: {t.get('authority') or '—'} | Plats: {t.get('region') or '—'}")
    lines.append(f"Källa: {provider.get('name', src)} | Publicerad: {(t.get('published_at') or '—')[:10]}")

    if t.get("deadline"):
        days = _days_until(t.get("deadline"))
        if days is not None:
            if days < 0:
                lines.append(f"Deadline: {t['deadline'][:10]} — STÄNGD ({abs(days)} dagar sedan)")
            elif days <= 3:
                lines.append(f"Deadline: {t['deadline'][:10]} — ⚠️ BRÅDSKANDE ({days} dagar kvar)")
            elif days <= 14:
                lines.append(f"Deadline: {t['deadline'][:10]} — {days} dagar kvar (snart)")
            else:
                lines.append(f"Deadline: {t['deadline'][:10]} — {days} dagar kvar")

    if t.get("value"):
        lines.append(f"Värde: {t['value']:,.0f} SEK")
    if t.get("procedure"):
        lines.append(f"Procedur: {t['procedure']}")
    if t.get("contract_type"):
        lines.append(f"Avtalstyp: {t['contract_type']}")
    if t.get("cpv_codes"):
        cpv = t["cpv_codes"] if isinstance(t["cpv_codes"], list) else []
        if cpv:
            lines.append(f"CPV: {', '.join(str(c) for c in cpv[:5])}")

    lines.append("")
    lines.append(f"🔗 Länk: {t.get('tender_url') or t.get('source_url') or '—'}")
    # Per-source guidance so an agent knows what it can do at the link
    if src == "mercell":
        lines.append(
            "ℹ️  Annonsen på länken är publik. Bilagor/upphandlingsdokument och "
            "anbudsinlämning kräver inloggat Mercell-konto — om din användare har "
            "ett: logga in, öppna länken och hämta bilagorna under 'Documents'."
        )
    elif src in ("ted", "ted_awards", "ted_pin"):
        lines.append(
            "✅ TED är helt publikt — hela annonsen syns utan konto. "
            "Upphandlingsdokumenten ligger hos upphandlarens plattform: leta efter "
            "'Address of the procurement documents' i annonsen och följ den länken "
            "(nedladdning kan kräva konto där, t.ex. TendSign eller Mercell)."
        )
    elif src == "lov":
        lines.append("ℹ️  LOV: löpande ansökan utan deadline. Ansökan görs hos kommunen via länken.")

    if t.get("description"):
        desc = t["description"][:400]
        lines.append(f"\n{desc}{'...' if len(t.get('description','')) > 400 else ''}")
    return "\n".join(lines)


# ----- Tool definitions ------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_tenders",
            description=(
                "Search Swedish public procurement tenders. "
                "Examples: query='IT-konsult stockholm', cpv='72' (IT), cpv='45' (construction), "
                "source='ted' (EU-thresholds only), open_only=false (include closed). "
                "Returns title, buyer, deadline with days-until, value, CPV, and a deep link."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword. Swedish works best. Examples: 'IT-konsult', 'vägbyggnation', 'solcell', 'städning'."
                    },
                    "source": {
                        "type": "string",
                        "enum": ["mercell", "ted"],
                        "description": "Data source to filter by. 'mercell' = most Swedish tenders. 'ted' = EU-threshold only."
                    },
                    "authority": {
                        "type": "string",
                        "description": "Filter by buyer/contracting authority (substring match). Examples: 'Trafikverket', 'Stockholms kommun', 'KTH'."
                    },
                    "cpv": {
                        "type": "string",
                        "description": "CPV code prefix to filter by. Examples: '72' (IT), '45' (construction), '34' (transport), '33' (medical), '09' (energy)."
                    },
                    "open_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true (default), exclude tenders past their deadline."
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Max results (default 10, max 50)."
                    }
                }
            },
        ),
        types.Tool(
            name="get_tender",
            description=(
                "Get full details for one tender by its internal id (from search_tenders). "
                "Includes complete description, deadline and the link to the original notice. "
                "To fetch documents/attachments: open tender_url. TED notices are fully "
                "public (procurement documents linked inside the notice); Mercell shows "
                "the notice publicly but downloading attachments requires a logged-in "
                "Mercell account."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Internal tender id."}
                },
                "required": ["id"]
            },
        ),
        types.Tool(
            name="get_stats",
            description=(
                "Database overview: total tenders, open count, per-source counts, last sync. "
                "Use this first to understand what's available before searching."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_providers",
            description=(
                "List data sources (Mercell, TED EU, etc.) with status and whether they require "
                "an account to APPLY. Note: data is always free; the account is only for submission."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_regions",
            description=(
                "List Swedish regions (län) with tender counts. Use before search_tenders to "
                "discover geographic coverage."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="sync_now",
            description=(
                "Trigger immediate scrape of all enabled sources. Returns when sync starts; "
                "check get_stats after 60-90s to see updated counts."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_cpv_top",
            description=(
                "Top CPV (Common Procurement Vocabulary) codes in the database with counts. "
                "Use this to discover what categories have tenders before searching. "
                "Examples: prefix='72' for IT-only top categories, top=5 for top 5 overall."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "Optional CPV prefix to filter (e.g. '72' for IT, '45' for construction)."
                    },
                    "top": {
                        "type": "integer",
                        "default": 15,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "How many top categories to return (default 15)."
                    }
                }
            },
        ),
        types.Tool(
            name="search_knowledge",
            description=(
                "Search the knowledge base — sustainability criteria (hållbarhetskriterier) "
                "and Q&A (juridiska frågor) from Upphandlingsmyndigheten. "
                "Use when the user asks about specific rules, environmental requirements, "
                "or LOU/LOV interpretations. NOT for live tenders — use search_tenders for that. "
                "Examples: q='IT-miljö', q='LOU tröskelvärde', source='criteria'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "Search terms. Searches title, excerpt, and tags. Example: 'IT avfall'."
                    },
                    "source": {
                        "type": "string",
                        "enum": ["criteria", "questions"],
                        "description": "Optional: filter to one type. 'criteria' = sustainability, 'questions' = Q&A."
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional: filter by primary category, e.g. 'IT och telekom'."
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "How many results to return (default 10, max 50)."
                    }
                },
                "required": ["q"]
            },
        ),
        types.Tool(
            name="get_knowledge",
            description=(
                "Get full details of a single knowledge item by id, including all tags and the source URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The knowledge item id (from search_knowledge results)."
                    }
                },
                "required": ["id"]
            },
        ),
        types.Tool(
            name="get_authority",
            description=(
                "All tenders from one specific buyer/contracting authority. "
                "Use search_tenders first to find a buyer name, then get_authority for their full list. "
                "Examples: name='Trafikverket', name='Stockholms kommun'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Buyer/authority name (substring match). Examples: 'Trafikverket', 'KTH', 'Mälarenergi'."
                    },
                    "open_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, exclude past-deadline tenders."
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Max results (default 20, max 100)."
                    }
                },
                "required": ["name"]
            },
        ),
        types.Tool(
            name="match_profile",
            description=(
                "Find tenders matching a profile (keywords + CPV prefixes + regions). "
                "Use this for monitoring/saved searches. "
                "Examples: keywords=['IT', 'digitalisering'], cpv_prefixes=['72'], regions=['Stockholms län']."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keywords to match against title+description. Any-match (OR)."
                    },
                    "cpv_prefixes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CPV prefixes to match. Examples: ['72', '722']."
                    },
                    "regions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Region names to match. Examples: ['Stockholms län', 'Västra Götalands län']."
                    },
                    "open_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true (default), only open tenders."
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Max results."
                    }
                }
            },
        ),
        types.Tool(
            name="get_winner_history",
            description=(
                "Who tends to WIN contracts in a given area — market intelligence from TED "
                "award notices. Filter by buyer/authority and/or CPV prefix. Returns the "
                "suppliers ranked by number of awards won, with total awarded value. "
                "Use this to answer 'is it worth bidding, or does the same supplier always win?'. "
                "Examples: authority='Trafikverket'; cpv='45' (construction); authority='Region Stockholm', cpv='85'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "authority": {
                        "type": "string",
                        "description": "Buyer/authority name (substring match). Example: 'Trafikverket'."
                    },
                    "cpv": {
                        "type": "string",
                        "description": "CPV code prefix. Examples: '45' (construction), '72' (IT), '85' (health)."
                    },
                    "top": {
                        "type": "integer",
                        "default": 15,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "How many top winners to return (default 15)."
                    }
                }
            },
        ),
        types.Tool(
            name="similar_tenders",
            description=(
                "Find tenders similar to a given one — same CPV categories and/or same buyer. "
                "Use after search_tenders/get_tender when the user likes one and wants more like it. "
                "Ranks by shared CPV codes (weighted) plus a bonus for the same authority. "
                "Example: similar_tenders(id=142)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Tender id to find similar ones for (from search_tenders)."},
                    "open_only": {"type": "boolean", "default": True, "description": "If true (default), only open tenders."},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30, "description": "Max results (default 10)."}
                },
                "required": ["id"]
            },
        ),
        types.Tool(
            name="deadline_calendar",
            description=(
                "Upcoming tender deadlines within N days, soonest first — for planning what to bid on. "
                "Optionally filter by CPV prefix and/or buyer. Groups by this week / this month so an "
                "agent can flag urgency. Example: deadline_calendar(days=14, cpv='72')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365, "description": "Look-ahead window in days (default 30)."},
                    "cpv": {"type": "string", "description": "Optional CPV prefix filter. '45'=construction, '72'=IT."},
                    "authority": {"type": "string", "description": "Optional buyer name (substring match)."},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100, "description": "Max tenders to list (default 25)."}
                }
            },
        ),
    ]


# ----- Tool implementations -------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.Content]:
    conn = connect(DB_PATH)
    try:
        if name == "search_tenders":
            return await _search_tenders(conn, arguments)
        elif name == "get_tender":
            return await _get_tender(conn, arguments)
        elif name == "get_stats":
            return await _get_stats(conn, arguments)
        elif name == "list_providers":
            return await _list_providers(conn, arguments)
        elif name == "list_regions":
            return await _list_regions(conn, arguments)
        elif name == "sync_now":
            return await _sync_now(arguments)
        elif name == "list_cpv_top":
            return await _list_cpv_top(conn, arguments)
        elif name == "get_authority":
            return await _get_authority(conn, arguments)
        elif name == "match_profile":
            return await _match_profile(conn, arguments)
        elif name == "search_knowledge":
            return await _search_knowledge(conn, arguments)
        elif name == "get_knowledge":
            return await _get_knowledge(conn, arguments)
        elif name == "get_winner_history":
            return await _get_winner_history(conn, arguments)
        elif name == "similar_tenders":
            return await _similar_tenders(conn, arguments)
        elif name == "deadline_calendar":
            return await _deadline_calendar(conn, arguments)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    finally:
        conn.close()


async def _search_tenders(conn, args: dict) -> list[types.Content]:
    where = []
    params: list = []

    if args.get("query"):
        where.append("(title LIKE ? OR description LIKE ?)")
        params.extend([f"%{args['query']}%", f"%{args['query']}%"])
    if args.get("source"):
        where.append("source_system = ?")
        params.append(args["source"])
    if args.get("authority"):
        where.append("authority LIKE ?")
        params.append(f"%{args['authority']}%")
    if args.get("cpv"):
        where.append("cpv_codes LIKE ?")
        params.append(f'%"{args["cpv"]}%')

    if args.get("open_only", True):
        where.append("(deadline IS NULL OR deadline > ?)")
        params.append(datetime.now().isoformat(timespec="seconds"))

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = min(args.get("limit", 10), 50)

    rows = conn.execute(
        f"""
        SELECT id, source_system, source_id, tender_url, title, authority,
               cpv_codes, deadline, published_at, value, procedure, region
        FROM tenders {where_sql}
        ORDER BY
            CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
            deadline ASC NULLS LAST,
            published_at DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    items = [_row_dict(r) for r in rows]

    if not items:
        return [types.TextContent(
            type="text",
            text=f"Inga upphandlingar matchar {args}. Prova bredare sökning, annan CPV, eller open_only=false."
        )]

    header = f"Hittade {len(items)} upphandlingar"
    body = "\n\n---\n\n".join(_format_tender(t) for t in items)
    return [types.TextContent(type="text", text=f"{header}\n\n{body}")]


async def _get_tender(conn, args: dict) -> list[types.Content]:
    tid = args.get("id")
    if not isinstance(tid, int):
        return [types.TextContent(type="text", text="Missing or invalid 'id' (must be integer).")]
    row = conn.execute("SELECT * FROM tenders WHERE id = ?", (tid,)).fetchone()
    if not row:
        return [types.TextContent(type="text", text=f"Tender {tid} not found.")]
    t = _row_dict(row)
    body = _format_tender(t)
    if t.get("description"):
        body += f"\n\n=== FULL DESCRIPTION ===\n{t['description']}"
    return [types.TextContent(type="text", text=body)]


async def _get_stats(conn, args: dict) -> list[types.Content]:
    total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    open_n = conn.execute(
        "SELECT COUNT(*) FROM tenders WHERE deadline IS NULL OR deadline > ?",
        (datetime.now().isoformat(timespec="seconds"),),
    ).fetchone()[0]
    by_source = conn.execute(
        "SELECT source_system, COUNT(*) FROM tenders GROUP BY source_system ORDER BY 2 DESC"
    ).fetchall()
    last = conn.execute(
        "SELECT source, run_at, count, status FROM sync_log ORDER BY run_at DESC LIMIT 1"
    ).fetchone()

    lines = [
        f"**Totalt:** {total} upphandlingar ({open_n} öppna just nu).",
        "",
        "**Per datakälla:**",
    ]
    for s, n in by_source:
        prov = PROVIDERS.get(s, {})
        auth = prov.get("auth", "?")
        auth_str = "öppet" if auth == "open" else "konto krävs"
        lines.append(f"- {prov.get('name', s)} [{s}]: {n} upphandlingar. Att ansöka: {auth_str}.")
    if last:
        ls = dict(last)
        lines.append("")
        lines.append(
            f"**Senaste sync:** {ls.get('source')} kl {ls.get('run_at','?')[:19]} — "
            f"{ls.get('count')} records, status={ls.get('status')}"
        )
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _list_providers(conn, args: dict) -> list[types.Content]:
    counts = dict(conn.execute(
        "SELECT source_system, COUNT(*) FROM tenders GROUP BY source_system"
    ).fetchall())
    lines = ["**Aktiva providers (live i Agentanbud):**", ""]
    for pid, meta in PROVIDERS.items():
        if meta["data_status"] != "live":
            continue
        n = counts.get(pid, 0)
        auth = "öppet" if meta["auth"] == "open" else "konto krävs"
        lines.append(
            f"- **{meta['name']}** [{pid}]: {n} upphandlingar i DB. "
            f"Att ansöka: {auth}. URL: {meta['url']}"
        )
    lines.append("")
    lines.append("**Planerade (ännu inte implementerade):**")
    for pid, meta in PROVIDERS.items():
        if meta["data_status"] == "live":
            continue
        lines.append(f"- {meta['name']} [{pid}] — auth: {meta['auth']}, status: {meta['data_status']}")
    lines.append("")
    lines.append("**Viktigt:** Att *läsa* data är alltid gratis. Att *ansöka* kräver ofta konto hos plattformen.")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _list_regions(conn, args: dict) -> list[types.Content]:
    rows = conn.execute(
        "SELECT region, COUNT(*) AS n FROM tenders "
        "WHERE region IS NOT NULL AND region != '' "
        "GROUP BY region ORDER BY n DESC LIMIT 30"
    ).fetchall()
    text = "**Regioner (län) i databasen:**\n\n"
    for region, n in rows:
        text += f"- {region}: {n}\n"
    if not rows:
        text += "(inga ännu)\n"
    text += "\nFör kommuner: search_tenders med authority='Stockholms kommun' (t.ex)."
    return [types.TextContent(type="text", text=text)]


async def _sync_now(args: dict) -> list[types.Content]:
    try:
        subprocess.Popen(
            ["python", "-m", "scraper.orchestrator"],
            cwd="/app",
            stdout=open("/var/log/agentanbud.log", "a"),
            stderr=subprocess.STDOUT,
        )
        return [types.TextContent(
            type="text",
            text="Sync startad. Vänta ~60-90s, kör sedan get_stats för att se uppdaterade counts."
        )]
    except FileNotFoundError:
        return [types.TextContent(
            type="text",
            text="Sync kunde inte startas — kör utanför Docker-containern? Starta manuellt med: python -m scraper.orchestrator"
        )]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Fel vid sync: {e}")]



async def _list_cpv_top(conn, args: dict) -> list[types.Content]:
    """Top CPV codes in the DB. Since cpv_codes is a JSON list, we extract
    each one and count occurrences."""
    rows = conn.execute(
        "SELECT cpv_codes FROM tenders WHERE cpv_codes IS NOT NULL AND cpv_codes != ''"
    ).fetchall()
    from collections import Counter
    counts: Counter = Counter()
    for r in rows:
        try:
            cpvs = json.loads(r[0])
            for c in cpvs:
                counts[c] += 1
        except Exception:
            pass

    prefix = args.get("prefix", "")
    if prefix:
        counts = Counter({k: v for k, v in counts.items() if k.startswith(prefix)})

    top = min(args.get("top", 15), 50)
    items = counts.most_common(top)
    if not items:
        return [types.TextContent(type="text", text="Inga CPV-koder hittades.")]
    lines = [f"**Top {len(items)} CPV-koder**" + (f" (prefix='{prefix}')" if prefix else "") + ":"]
    for code, n in items:
        lines.append(f"- `{code}`: {n} upphandlingar")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _get_authority(conn, args: dict) -> list[types.Content]:
    name = args.get("name")
    if not name:
        return [types.TextContent(type="text", text="Missing 'name'.")]
    where = ["authority LIKE ?"]
    params = [f"%{name}%"]
    if args.get("open_only", False):
        where.append("(deadline IS NULL OR deadline > ?)")
        params.append(datetime.now().isoformat(timespec="seconds"))
    where_sql = "WHERE " + " AND ".join(where)
    limit = min(args.get("limit", 20), 100)
    rows = conn.execute(
        f"""
        SELECT id, source_system, tender_url, title, deadline, value, region, cpv_codes
        FROM tenders {where_sql}
        ORDER BY
            CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
            deadline ASC NULLS LAST
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    items = [_row_dict(r) for r in rows]
    if not items:
        return [types.TextContent(type="text", text=f"Inga upphandlingar hittades för '{name}'. Försök kortare namn.")]

    lines = [f"**{len(items)} upphandlingar från '{name}':**", ""]
    for t in items:
        deadline = t.get("deadline", "")
        days = _days_until(deadline)
        if days is not None:
            if days < 0:
                d_str = f"stängd {abs(days)}d sedan"
            else:
                d_str = f"{days}d kvar"
        else:
            d_str = "—"
        value = f"{t['value']:,.0f} SEK" if t.get("value") else "—"
        lines.append(f"- [{t['id']}] {t.get('title','(utan titel)')[:80]} ({d_str}, {value})")
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _get_winner_history(conn, args: dict) -> list[types.Content]:
    """Aggregate award notices to show who wins in a given area.

    winner_name is a JSON list (framework agreements have several winners),
    so we parse each row and count per supplier, summing awarded value.
    """
    from collections import Counter

    authority = (args.get("authority") or "").strip()
    cpv = (args.get("cpv") or "").strip()
    if not authority and not cpv:
        return [types.TextContent(
            type="text",
            text="Ange minst en filter: authority (upphandlare) och/eller cpv (kategori-prefix).",
        )]

    where = ["source_system = 'ted_awards'", "winner_name IS NOT NULL", "winner_name != ''"]
    params: list = []
    if authority:
        where.append("authority LIKE ?")
        params.append(f"%{authority}%")
    if cpv:
        where.append("cpv_codes LIKE ?")
        params.append(f'%"{cpv}%')

    rows = conn.execute(
        f"SELECT winner_name, value FROM tenders WHERE {' AND '.join(where)}",
        params,
    ).fetchall()

    if not rows:
        scope = " och ".join(filter(None, [
            f"upphandlare '{authority}'" if authority else "",
            f"CPV '{cpv}'" if cpv else "",
        ]))
        return [types.TextContent(
            type="text",
            text=f"Inga tilldelningar hittades för {scope}. Prova bredare filter eller kortare namn.",
        )]

    wins: Counter = Counter()
    value_by_winner: dict = {}
    contracts = 0
    for r in rows:
        try:
            winners = json.loads(r[0])
        except Exception:
            continue
        if not winners:
            continue
        contracts += 1
        # TED often carries a placeholder value of 1 (or 0) SEK when the real
        # award value isn't published — treat those as "unreported" so summed
        # totals stay honest instead of showing e.g. "8 vinster — 1 SEK totalt".
        val = r[1] if (r[1] and r[1] > 1) else None
        for w in winners:
            wins[w] += 1
            if val:
                value_by_winner[w] = value_by_winner.get(w, 0.0) + float(val)

    if not wins:
        return [types.TextContent(type="text", text="Tilldelningar hittades men utan namngivna vinnare.")]

    scope = ", ".join(filter(None, [
        f"upphandlare '{authority}'" if authority else "",
        f"CPV-prefix '{cpv}'" if cpv else "",
    ]))
    top = min(args.get("top", 15), 50)
    ranked = wins.most_common(top)
    lines = [
        f"**Vem vinner — {scope}**",
        f"Baserat på {contracts} tilldelningar ({len(wins)} unika leverantörer).",
        "",
    ]
    for i, (w, n) in enumerate(ranked, 1):
        tot = value_by_winner.get(w)
        val_str = f" — {tot:,.0f} SEK totalt".replace(",", " ") if tot else ""
        lines.append(f"{i}. **{w}** — {n} vinst{'er' if n != 1 else ''}{val_str}")
    lines.append("")
    lines.append("💡 Tips: en dominerande vinnare kan betyda hård konkurrens — men också "
                 "att marknaden är öppen för en utmanare. Använd get_authority för att se "
                 "kommande upphandlingar från samma köpare.")
    return [types.TextContent(type="text", text="\n".join(lines))]


def _as_cpv_list(raw) -> list:
    """Normalise a cpv_codes cell (JSON string or list) to a list of str."""
    if isinstance(raw, list):
        return [str(c) for c in raw]
    if isinstance(raw, str) and raw:
        try:
            return [str(c) for c in json.loads(raw)]
        except Exception:
            return []
    return []


async def _similar_tenders(conn, args: dict) -> list[types.Content]:
    """Find tenders similar to a given one by shared CPV codes + same buyer."""
    tid = args.get("id")
    if not isinstance(tid, int):
        return [types.TextContent(type="text", text="Missing or invalid 'id' (must be integer).")]
    src_row = conn.execute(
        "SELECT id, title, authority, cpv_codes FROM tenders WHERE id = ?", (tid,)
    ).fetchone()
    if not src_row:
        return [types.TextContent(type="text", text=f"Tender {tid} not found.")]
    src = _row_dict(src_row)
    src_cpvs = _as_cpv_list(src.get("cpv_codes"))
    src_auth = (src.get("authority") or "").strip()

    # Candidate fetch: share a CPV group (first 4 digits) OR same buyer.
    prefixes = sorted({c[:4] for c in src_cpvs if c})
    clauses, params = [], []
    for p in prefixes:
        clauses.append("cpv_codes LIKE ?")
        params.append(f'%"{p}%')
    if src_auth:
        clauses.append("authority = ?")
        params.append(src_auth)
    if not clauses:
        return [types.TextContent(
            type="text",
            text=f"Upphandling #{tid} saknar CPV-koder och upphandlare — går inte att hitta liknande.")]

    where = f"({' OR '.join(clauses)}) AND id != ?"
    params.append(tid)
    if args.get("open_only", True):
        where += " AND (deadline IS NULL OR deadline > ?)"
        params.append(datetime.now().isoformat(timespec="seconds"))

    rows = conn.execute(
        f"""SELECT id, source_system, source_id, tender_url, title, authority,
                   cpv_codes, deadline, value, region
            FROM tenders WHERE {where} LIMIT 300""",
        params,
    ).fetchall()

    src_set = set(src_cpvs)
    scored = []
    for r in rows:
        t = _row_dict(r)
        shared = len(src_set & set(_as_cpv_list(t.get("cpv_codes"))))
        score = shared * 2 + (3 if (t.get("authority") or "").strip() == src_auth and src_auth else 0)
        if score > 0:
            scored.append((score, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    limit = min(args.get("limit", 10), 30)
    top = [t for _, t in scored[:limit]]

    if not top:
        return [types.TextContent(
            type="text",
            text=f"Inga liknande upphandlingar hittades för #{tid}. Prova open_only=false för att inkludera stängda.")]

    header = f"{len(top)} upphandlingar liknande #{tid} — {src.get('title') or '(utan titel)'}"[:120]
    body = "\n\n---\n\n".join(_format_tender(t) for t in top)
    return [types.TextContent(type="text", text=f"{header}\n\n{body}")]


async def _deadline_calendar(conn, args: dict) -> list[types.Content]:
    """Upcoming deadlines within N days, soonest first, with urgency buckets."""
    days = min(max(int(args.get("days", 30)), 1), 365)
    now = datetime.now()
    cutoff = (now + timedelta(days=days)).isoformat(timespec="seconds")

    where = ["deadline IS NOT NULL", "deadline > ?", "deadline <= ?"]
    params: list = [now.isoformat(timespec="seconds"), cutoff]
    cpv = (args.get("cpv") or "").strip()
    authority = (args.get("authority") or "").strip()
    if cpv:
        where.append("cpv_codes LIKE ?")
        params.append(f'%"{cpv}%')
    if authority:
        where.append("authority LIKE ?")
        params.append(f"%{authority}%")

    limit = min(int(args.get("limit", 25)), 100)
    rows = conn.execute(
        f"""SELECT id, source_system, source_id, tender_url, title, authority,
                   cpv_codes, deadline, value, region
            FROM tenders WHERE {' AND '.join(where)}
            ORDER BY deadline ASC LIMIT ?""",
        params + [limit],
    ).fetchall()

    scope = ", ".join(filter(None, [
        f"CPV '{cpv}'" if cpv else "",
        f"upphandlare '{authority}'" if authority else "",
    ]))
    scope_str = f" ({scope})" if scope else ""
    if not rows:
        return [types.TextContent(
            type="text",
            text=f"Inga upphandlingar stänger inom {days} dagar{scope_str}.")]

    week = month = 0
    tender_lines = []
    for r in rows:
        t = _row_dict(r)
        d = _days_until(t.get("deadline"))
        if d is None:
            continue
        if d <= 7:
            week += 1
        if d <= 30:
            month += 1
        flag = "⚠️ " if d <= 3 else ""
        value = f" · {t['value']:,.0f} SEK".replace(",", " ") if t.get("value") else ""
        tender_lines.append(
            f"- {flag}**{d}d** — {(t.get('title') or '(utan titel)')[:70]} "
            f"[{t.get('authority') or '—'}]{value}  (#{t['id']})"
        )
    lines = [
        f"**Deadlines inom {days} dagar{scope_str}** — {len(tender_lines)} upphandlingar, snarast först:",
        f"⏰ {week} stänger inom en vecka, {month} inom en månad.",
        "",
        *tender_lines,
    ]
    return [types.TextContent(type="text", text="\n".join(lines))]


async def _match_profile(conn, args: dict) -> list[types.Content]:
    """Match tenders against a profile: keywords (any-match) + CPV prefixes + regions."""
    where = []
    params: list = []

    keywords = args.get("keywords", [])
    if keywords:
        kw_ors = " OR ".join(["(title LIKE ? OR description LIKE ?)" for _ in keywords])
        where.append(f"({kw_ors})")
        for kw in keywords:
            params.extend([f"%{kw}%", f"%{kw}%"])

    cpv_prefixes = args.get("cpv_prefixes", [])
    if cpv_prefixes:
        cpv_ors = " OR ".join(["cpv_codes LIKE ?" for _ in cpv_prefixes])
        where.append(f"({cpv_ors})")
        for pfx in cpv_prefixes:
            params.append(f'%"{pfx}%')

    regions = args.get("regions", [])
    if regions:
        reg_ands = " AND ".join(["region LIKE ?" for _ in regions])
        where.append(f"({reg_ands})")
        for r in regions:
            params.append(f"%{r}%")

    if args.get("open_only", True):
        where.append("(deadline IS NULL OR deadline > ?)")
        params.append(datetime.now().isoformat(timespec="seconds"))

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = min(args.get("limit", 20), 50)
    rows = conn.execute(
        f"""
        SELECT id, source_system, tender_url, title, authority, deadline, value, region, cpv_codes
        FROM tenders {where_sql}
        ORDER BY
            CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
            deadline ASC NULLS LAST
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    items = [_row_dict(r) for r in rows]
    if not items:
        profile_str = ", ".join(filter(None, [
            f"keywords={keywords}" if keywords else "",
            f"cpv={cpv_prefixes}" if cpv_prefixes else "",
            f"regions={regions}" if regions else "",
        ]))
        return [types.TextContent(type="text", text=f"Inga matchande upphandlingar för {profile_str}.")]

    header = f"**{len(items)} matchande upphandlingar** (profil: {args})"
    body = "\n\n---\n\n".join(_format_tender(t) for t in items)
    return [types.TextContent(type="text", text=f"{header}\n\n{body}")]


def _format_knowledge(k: dict) -> str:
    """Format one knowledge item for LLM consumption."""
    type_label = "Kriterium" if k["source_system"] == "criteria" else "Fråga"
    lines = [f"**[{k['id']}] {type_label}: {k.get('title', '(ingen titel)')}**"]
    if k.get("category"):
        lines.append(f"Kategori: {k['category']}")
    if k.get("subcategory") and k.get("subcategory") != k.get("category"):
        lines.append(f"Subkategori: {k['subcategory']}")
    if k.get("tags"):
        lines.append(f"Taggar: {', '.join(k['tags'][:5])}")
    if k.get("excerpt"):
        lines.append(f"\n{k['excerpt'][:500]}")
    if k.get("url"):
        lines.append(f"\nLäs mer: {k['url']}")
    return "\n".join(lines)


async def _search_knowledge(conn, args: dict) -> list[types.Content]:
    """Search the knowledge base — criteria or Q&A."""
    where = []
    params: list = []
    q = args.get("q", "")
    if not q:
        return [types.TextContent(type="text", text="Missing 'q' (search term).")]
    where.append("(title LIKE ? OR excerpt LIKE ? OR tags LIKE ?)")
    params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if args.get("source"):
        where.append("source_system = ?")
        params.append(args["source"])
    if args.get("category"):
        where.append("(category = ? OR subcategory = ?)")
        params.extend([args["category"], args["category"]])
    where_sql = "WHERE " + " AND ".join(where)
    limit = min(args.get("limit", 10), 50)
    rows = conn.execute(
        f"""
        SELECT id, source_system, source_id, url, title, category, subcategory,
               tags, excerpt
        FROM knowledge {where_sql}
        ORDER BY source_system, id
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    items = [_row_dict(r) for r in rows]
    if not items:
        return [types.TextContent(
            type="text",
            text=f"Inga kunskapsresultat för '{q}'. Prova andra termer eller ta bort filter."
        )]
    src_label = args.get("source", "alla typer")
    header = f"**{len(items)} kunskapsresultat för '{q}'** (typ: {src_label})"
    body = "\n\n---\n\n".join(_format_knowledge(k) for k in items)
    return [types.TextContent(type="text", text=f"{header}\n\n{body}")]


async def _get_knowledge(conn, args: dict) -> list[types.Content]:
    """Get full details of a single knowledge item."""
    kid = args.get("id")
    if not isinstance(kid, int):
        return [types.TextContent(type="text", text="Missing or invalid 'id' (must be integer).")]
    row = conn.execute(
        "SELECT id, source_system, source_id, url, title, category, subcategory, "
        "tags, excerpt, body, fetched_at FROM knowledge WHERE id = ?",
        (kid,),
    ).fetchone()
    if not row:
        return [types.TextContent(type="text", text=f"Knowledge item {kid} not found.")]
    k = _row_dict(row)
    return [types.TextContent(type="text", text=_format_knowledge(k))]



# ----- Entry point ----------------------------------------------------------

async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
