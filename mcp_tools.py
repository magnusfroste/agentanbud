"""
Tool definitions — the single source of truth for both MCP transports.

`mcp_server.py` (stdio, local) and `mcp_http.py` (Streamable HTTP, public)
used to each carry their own hand-written copy of these schemas, which had
already drifted apart (the source enums disagreed). Both now import from
here so a tool is described identically no matter how an agent connects.

Each entry is a plain dict in MCP's tool shape:
    {"name": ..., "description": ..., "inputSchema": {...}}

`local_only=True` marks a tool that must NOT be exposed on the public HTTP
endpoint — currently just `sync_now`, since running it already requires
local/container access.
"""
from __future__ import annotations

# Data sources present in the DB. Kept as a constant so the enum can't drift
# out of sync with what the scrapers actually write.
SOURCES = ["mercell", "ted", "ted_awards", "ted_pin", "lov"]

TOOLS: list[dict] = [
    {
        "name": "search_tenders",
        "description": (
            "Search Swedish public procurement tenders. "
            "Examples: query='IT-konsult stockholm', cpv='72' (IT), cpv='45' (construction), "
            "source='ted' (EU-thresholds only), open_only=false (include closed). "
            "Returns title, buyer, deadline with days-until, value, CPV, and a deep link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword. Swedish works best. Examples: 'IT-konsult', 'vägbyggnation', 'solcell', 'städning'.",
                },
                "source": {
                    "type": "string",
                    "enum": SOURCES,
                    "description": "Data source filter. 'mercell' = most Swedish tenders, 'ted' = EU-threshold notices, 'ted_awards' = awarded contracts, 'ted_pin' = planned procurements, 'lov' = LOV services.",
                },
                "authority": {
                    "type": "string",
                    "description": "Filter by buyer/contracting authority (substring match). Examples: 'Trafikverket', 'Stockholms kommun', 'KTH'.",
                },
                "cpv": {
                    "type": "string",
                    "description": "CPV code prefix to filter by. Examples: '72' (IT), '45' (construction), '34' (transport), '33' (medical), '09' (energy).",
                },
                "open_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true (default), exclude tenders past their deadline.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Max results (default 10, max 50).",
                },
            },
        },
    },
    {
        "name": "get_tender",
        "description": (
            "Get full details for one tender by its internal id (from search_tenders). "
            "Includes complete description, deadline and the link to the original notice. "
            "To fetch documents/attachments: open tender_url. TED notices are fully "
            "public (procurement documents linked inside the notice); Mercell shows "
            "the notice publicly but downloading attachments requires a logged-in "
            "Mercell account."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Internal tender id."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_stats",
        "description": (
            "Database overview: total tenders, open count, per-source counts, last sync. "
            "Use this first to understand what's available before searching."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_providers",
        "description": (
            "List data sources (Mercell, TED EU, etc.) with status and whether they require "
            "an account to APPLY. Note: data is always free; the account is only for submission."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_regions",
        "description": (
            "List Swedish regions (län) with tender counts. Use before search_tenders to "
            "discover geographic coverage."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sync_now",
        "local_only": True,
        "description": (
            "Trigger immediate scrape of all enabled sources. Returns when sync starts; "
            "check get_stats after 60-90s to see updated counts."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_cpv_top",
        "description": (
            "Top CPV (Common Procurement Vocabulary) codes in the database with counts. "
            "Use this to discover what categories have tenders before searching. "
            "Examples: prefix='72' for IT-only top categories, top=5 for top 5 overall."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix": {
                    "type": "string",
                    "description": "Optional CPV prefix to filter (e.g. '72' for IT, '45' for construction).",
                },
                "top": {
                    "type": "integer",
                    "default": 15,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "How many top categories to return (default 15).",
                },
            },
        },
    },
    {
        "name": "search_knowledge",
        "description": (
            "Search the knowledge base — sustainability criteria (hållbarhetskriterier) "
            "and Q&A (juridiska frågor) from Upphandlingsmyndigheten. "
            "Use when the user asks about specific rules, environmental requirements, "
            "or LOU/LOV interpretations. NOT for live tenders — use search_tenders for that. "
            "Examples: q='IT-miljö', q='LOU tröskelvärde', source='criteria'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Search terms. Searches title, excerpt, and tags. Example: 'IT avfall'.",
                },
                "source": {
                    "type": "string",
                    "enum": ["criteria", "questions"],
                    "description": "Optional: filter to one type. 'criteria' = sustainability, 'questions' = Q&A.",
                },
                "category": {
                    "type": "string",
                    "description": "Optional: filter by primary category, e.g. 'IT och telekom'.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "How many results to return (default 10, max 50).",
                },
            },
            "required": ["q"],
        },
    },
    {
        "name": "get_knowledge",
        "description": (
            "Get full details of a single knowledge item by id, including all tags and the source URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The knowledge item id (from search_knowledge results).",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_authority",
        "description": (
            "All tenders from one specific buyer/contracting authority. "
            "Use search_tenders first to find a buyer name, then get_authority for their full list. "
            "Examples: name='Trafikverket', name='Stockholms kommun'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Buyer/authority name (substring match). Examples: 'Trafikverket', 'KTH', 'Mälarenergi'.",
                },
                "open_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, exclude past-deadline tenders.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Max results (default 20, max 100).",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "match_profile",
        "description": (
            "Find tenders matching a profile (keywords + CPV prefixes + regions). "
            "Use this for monitoring/saved searches. "
            "Examples: keywords=['IT', 'digitalisering'], cpv_prefixes=['72'], regions=['Stockholms län']."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords to match against title+description. Any-match (OR).",
                },
                "cpv_prefixes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CPV prefixes to match. Examples: ['72', '722'].",
                },
                "regions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Region names to match. Examples: ['Stockholms län', 'Västra Götalands län'].",
                },
                "open_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true (default), only open tenders.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Max results.",
                },
            },
        },
    },
]


def tool_dicts(include_local: bool = False) -> list[dict]:
    """Tool metadata in MCP wire shape, minus our own `local_only` marker."""
    return [
        {k: v for k, v in t.items() if k != "local_only"}
        for t in TOOLS
        if include_local or not t.get("local_only")
    ]
