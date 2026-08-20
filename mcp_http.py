"""
MCP server — HTTP transport (Streamable HTTP).

Exposes the same 10 tools as the stdio `mcp_server` but over HTTP/SSE so
remote clients (Claude Code, Cursor, Windsurf, future MCP-aware web
assistants) can connect with just a URL — no local install needed.

Endpoint: POST /mcp  (Content-Type: application/json or text/event-stream)

Per-request body is JSON-RPC 2.0:
  { "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {} }
  { "jsonrpc": "2.0", "id": 2, "method": "tools/call",
    "params": { "name": "search_tenders", "arguments": { "query": "IT" } } }

Response: 200 OK with JSON-RPC result, or 4xx with JSON-RPC error.

CORS: open for all origins (this is a public civic-tech API; rate limit
can be added later if abuse occurs).

Mounted from app/main.py via:
    from mcp_http import mcp_router
    app.include_router(mcp_router)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.db import connect, log_usage
import mcp_shared
import mcp_server
from mcp_shared import SOURCES, SOURCE_DESCRIPTION
from app.version import build_info, version_string

# Admin key gates the blog write tools on the public /mcp endpoint. Reads stay
# open for every visiting agent; writing (create_post/update_post) requires the
# same X-Admin-Key as the REST admin endpoints. With no key configured the
# write tools are hidden and refused — set ALLOW_OPEN_ADMIN=true to open them
# for local development.
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
ALLOW_OPEN_ADMIN = os.environ.get("ALLOW_OPEN_ADMIN", "").lower() in ("1", "true", "yes")
WRITE_TOOLS = {"create_post", "update_post"}


def _is_authed(request: Request) -> bool:
    """Whether the caller may use the blog write tools.

    Fails CLOSED when no key is configured, matching _require_admin: an unset
    ADMIN_API_KEY used to expose create_post/update_post to every visiting
    agent, so a mistyped env var in production silently handed the blog to
    anyone who found /mcp.
    """
    if not ADMIN_API_KEY:
        return ALLOW_OPEN_ADMIN
    return hmac.compare_digest(request.headers.get("x-admin-key", ""), ADMIN_API_KEY)

LOG = logging.getLogger(__name__)

# Per-process store of sessions. Each session is identified by a UUID
# the client supplies via mcp-session-id header (MCP spec for Streamable HTTP).
# Sessions are short-lived (default 1h) and stateful only for the duration
# of one client connection.
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 3600


def _evict_expired_sessions() -> None:
    """Drop sessions older than SESSION_TTL_SECONDS. Cheap to call per request."""
    now = time.time()
    expired = [sid for sid, s in SESSIONS.items() if now - s["last_seen"] > SESSION_TTL_SECONDS]
    for sid in expired:
        SESSIONS.pop(sid, None)


def _format_result(content_list) -> dict:
    """Convert list[types.TextContent] to JSON-RPC result envelope."""
    return {
        "content": [
            {"type": c.type, "text": c.text} for c in content_list
        ]
    }


def _extract_query(name: str, arguments: dict) -> str | None:
    """Pull the human-readable search term out of a tool call, for /analytics."""
    if name == "search_tenders":
        return arguments.get("query")
    if name == "search_knowledge":
        return arguments.get("q")
    if name in ("get_authority", "get_winner_history"):
        return arguments.get("authority") or arguments.get("name")
    if name == "match_profile":
        keywords = arguments.get("keywords") or []
        return ", ".join(keywords) if keywords else None
    return None


def _log_connect(client_info: dict, protocol: str | None, session_id: str | None) -> None:
    """Record an MCP handshake: which client connected, and on what protocol.

    Aggregate only — the client's self-reported name/version plus the same
    anonymous session hash the tool calls use. No IP, no token, nothing tied
    to a person. Best-effort: a logging failure must not break a handshake.
    """
    try:
        name = (client_info.get("name") or "okänd")[:64]
        version = (client_info.get("version") or "")[:32]
        conn = connect(mcp_server.DB_PATH)
        try:
            meta = {"client": name, "client_version": version,
                    "protocol": (protocol or "")[:24]}
            if session_id:
                meta["_sid"] = hashlib.sha256(session_id.encode()).hexdigest()[:12]
            # query stays None: it feeds the "most searched terms" stats, and
            # a client name is not a search term.
            log_usage(conn, "mcp", "mcp:connect", query=None, meta=meta)
        finally:
            conn.close()
    except Exception:
        LOG.exception("failed to log MCP connect")


async def _dispatch_tool(name: str, arguments: dict, session_id: str | None = None) -> list:
    """Call the right tool handler (reuses the stdio version's logic)."""
    conn = connect(mcp_server.DB_PATH)
    try:
        try:
            # Anonymous, one-way hash of the MCP session id — lets /analytics
            # count DISTINCT agent sessions (one agent makes many calls) without
            # storing the raw token or anything tied to a person.
            meta = dict(arguments or {})
            if session_id:
                meta["_sid"] = hashlib.sha256(session_id.encode()).hexdigest()[:12]
            log_usage(conn, "mcp", f"tool:{name}",
                      query=_extract_query(name, arguments), meta=meta or None)
        except Exception:
            LOG.exception("log_usage failed for MCP tool %s", name)
        if name == "search_tenders":
            return await mcp_server._search_tenders(conn, arguments or {})
        if name == "get_tender":
            return await mcp_server._get_tender(conn, arguments or {})
        if name == "get_stats":
            return await mcp_server._get_stats(conn, arguments or {})
        if name == "list_providers":
            return await mcp_server._list_providers(conn, arguments or {})
        if name == "list_regions":
            return await mcp_server._list_regions(conn, arguments or {})
        if name == "list_cpv_top":
            return await mcp_server._list_cpv_top(conn, arguments or {})
        if name == "get_authority":
            return await mcp_server._get_authority(conn, arguments or {})
        if name == "match_profile":
            return await mcp_server._match_profile(conn, arguments or {})
        if name == "search_knowledge":
            return await mcp_server._search_knowledge(conn, arguments or {})
        if name == "get_knowledge":
            return await mcp_server._get_knowledge(conn, arguments or {})
        if name == "get_usage_stats":
            return await mcp_server._get_usage_stats(conn, arguments or {})
        if name == "get_winner_history":
            return await mcp_server._get_winner_history(conn, arguments or {})
        if name == "similar_tenders":
            return await mcp_server._similar_tenders(conn, arguments or {})
        if name == "deadline_calendar":
            return await mcp_server._deadline_calendar(conn, arguments or {})
        if name == "list_posts":
            return await mcp_server._list_posts(conn, arguments or {})
        if name == "get_post":
            return await mcp_server._get_post(conn, arguments or {})
        if name == "get_post_stats":
            return await mcp_server._get_post_stats(conn, arguments or {})
        if name == "create_post":
            return await mcp_server._create_post(conn, arguments or {})
        if name == "update_post":
            return await mcp_server._update_post(conn, arguments or {})
        raise ValueError(f"Unknown tool: {name}")
    finally:
        conn.close()


def _tool_list(include_write: bool = False) -> list[dict]:
    """Mirror of mcp_server's @server.list_tools() — return tool metadata as dicts.

    The full Tool objects are defined in mcp_server.py. We rebuild the
    same shape here as dicts (no need to import pydantic types).

    Read tools are always listed (open for every visiting agent). The blog
    write tools (create_post/update_post) are appended only when the caller
    is authenticated with the admin key.
    """
    tools = [
        {
            "name": "search_tenders",
            "description": "Search Swedish public procurement tenders. Examples: query='IT-konsult stockholm', cpv='72' (IT), cpv='45' (construction), source='ted' (EU-thresholds only), open_only=false (include closed). Returns title, buyer, deadline with days-until, value, CPV, and a deep link.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "buyer_type": {
                        "type": "string",
                        "enum": mcp_shared.BUYER_TYPES,
                        "description": mcp_shared.BUYER_TYPE_DESCRIPTION,
                    },
                    "query": {"type": "string", "description": "Search keyword. Swedish works best."},
                    "source": {"type": "string", "enum": SOURCES, "description": SOURCE_DESCRIPTION},
                    "authority": {"type": "string", "description": "Filter by buyer/contracting authority (substring match)."},
                    "cpv": {"type": "string", "description": "CPV code prefix. '72'=IT, '45'=construction."},
                    "open_only": {"type": "boolean", "default": True, "description": "If true (default), exclude tenders past their deadline."},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50}
                }
            }
        },
        {
            "name": "get_tender",
            "description": "Get full details of one tender by id, including description, CPV codes, and the link to the original notice. To fetch documents/attachments, open tender_url: TED notices are fully public (procurement documents linked inside the notice); Mercell shows the notice publicly but downloading attachments requires a logged-in Mercell account.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "Tender id from search_tenders."}},
                "required": ["id"]
            }
        },
        {
            "name": "get_stats",
            "description": "Get overview: total tenders, open count, per-source breakdown, last sync.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "list_providers",
            "description": "List data sources with status and paywall info. Note: data is always free; account is only for submission.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "list_regions",
            "description": "List Swedish regions (län) with tender counts. Use before search_tenders to discover geographic coverage.",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "list_cpv_top",
            "description": "Top CPV codes with counts. prefix='72' filters to IT-only, top=5 for top 5.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Optional CPV prefix filter (e.g. '72' for IT)."},
                    "top": {"type": "integer", "default": 15, "minimum": 1, "maximum": 50}
                }
            }
        },
        {
            "name": "get_usage_stats",
            "description": "Usage figures for Agentanbud itself: visits (human vs crawler), searches, MCP agent calls, demand per industry segment, top search terms and zero-result searches. For reporting or writing about how the site is used. days=1 for 24h, omit for all time. Aggregate only, no personal data — free to publish.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "Look back this many days. Omit for all time."}
                }
            }
        },
        {
            "name": "get_authority",
            "description": "All tenders from one specific buyer. name='Trafikverket' (substring match).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Buyer name (substring)."},
                    "open_only": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}
                },
                "required": ["name"]
            }
        },
        {
            "name": "match_profile",
            "description": "Match tenders against a profile (keywords + CPV prefixes + regions).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "cpv_prefixes": {"type": "array", "items": {"type": "string"}},
                    "regions": {"type": "array", "items": {"type": "string"}},
                    "open_only": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50}
                }
            }
        },
        {
            "name": "search_knowledge",
            "description": "Search the knowledge base — sustainability criteria + Q&A from Upphandlingsmyndigheten. Use for rules/requirements/LOU/LOV questions. NOT for live tenders.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search terms. Swedish preferred."},
                    "source": {"type": "string", "enum": ["criteria", "questions"]},
                    "category": {"type": "string"},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50}
                },
                "required": ["q"]
            }
        },
        {
            "name": "get_knowledge",
            "description": "Get full details of one knowledge item by id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"]
            }
        },
        {
            "name": "get_winner_history",
            "description": "Who tends to WIN contracts in a given area — market intelligence from TED award notices. Filter by authority and/or CPV prefix; returns suppliers ranked by awards won with total value. Answers 'is it worth bidding, or does the same supplier always win?'. Examples: authority='Trafikverket'; cpv='45'.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "authority": {"type": "string", "description": "Buyer/authority name (substring). Example: 'Trafikverket'."},
                    "cpv": {"type": "string", "description": "CPV code prefix. '45'=construction, '72'=IT, '85'=health."},
                    "top": {"type": "integer", "default": 15, "minimum": 1, "maximum": 50, "description": "How many top winners (default 15)."}
                }
            }
        },
        {
            "name": "similar_tenders",
            "description": "Find tenders similar to a given one — same CPV categories and/or same buyer. Use after search_tenders/get_tender when the user likes one and wants more like it. Example: similar_tenders(id=142).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Tender id to find similar ones for."},
                    "open_only": {"type": "boolean", "default": True, "description": "If true (default), only open tenders."},
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30, "description": "Max results (default 10)."}
                },
                "required": ["id"]
            }
        },
        {
            "name": "deadline_calendar",
            "description": "Upcoming tender deadlines within N days, soonest first — for planning what to bid on. Optional CPV/buyer filter; groups by this week / this month. Example: deadline_calendar(days=14, cpv='72').",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365, "description": "Look-ahead window in days (default 30)."},
                    "cpv": {"type": "string", "description": "Optional CPV prefix. '45'=construction, '72'=IT."},
                    "authority": {"type": "string", "description": "Optional buyer name (substring)."},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100, "description": "Max tenders to list (default 25)."}
                }
            }
        },
        {
            "name": "list_posts",
            "description": "List published blog posts about Swedish public procurement (newest first). Optional tag filter.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Optional tag filter."},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50}
                }
            }
        },
        {
            "name": "get_post",
            "description": "Get one blog post by slug, including its Markdown body and engagement stats.",
            "inputSchema": {
                "type": "object",
                "properties": {"slug": {"type": "string", "description": "Post slug (from list_posts)."}},
                "required": ["slug"]
            }
        },
        {
            "name": "get_post_stats",
            "description": "Engagement stats per blog post — views, full-reads, read-rate. Omit slug for all posts. Use this to see what resonated and pick topics for the next post.",
            "inputSchema": {
                "type": "object",
                "properties": {"slug": {"type": "string", "description": "Optional: one post's slug. Omit for all."}}
            }
        },
    ]
    if include_write:
        tools += [
            {
                "name": "create_post",
                "description": "Publish a new blog post (admin only). Write body_md in Markdown. Use for a daily procurement-news post; check get_post_stats first to see what engages readers.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Post title."},
                        "body_md": {"type": "string", "description": "Post body in Markdown."},
                        "summary": {"type": "string", "description": "Short excerpt for the list view and social preview."},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags, e.g. ['LOU','IT']."},
                        "image_url": {"type": "string", "description": "Optional hero image — a plain https:// URL you have the right to use."}
                    },
                    "required": ["title", "body_md"]
                }
            },
            {
                "name": "update_post",
                "description": "Update an existing blog post by slug (admin only). Set status='draft' to unpublish.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Post slug to update."},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "body_md": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "image_url": {"type": "string", "description": "Hero image (https URL); empty string removes it."},
                        "status": {"type": "string", "enum": ["published", "draft"]}
                    },
                    "required": ["slug"]
                }
            },
        ]
    return tools


# ----- FastAPI router -------------------------------------------------------

mcp_router = APIRouter()


def _cors_headers() -> dict:
    """CORS for browser-based MCP clients (Claude.ai, future web tools)."""
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Accept, mcp-session-id, x-admin-key",
        "Access-Control-Max-Age": "86400",
    }


@mcp_router.options("/mcp")
async def mcp_options():
    """CORS preflight."""
    return Response(status_code=204, headers=_cors_headers())


@mcp_router.get("/mcp")
async def mcp_get(request: Request):
    """MCP server info — clients call this to discover capabilities."""
    _evict_expired_sessions()
    return JSONResponse({
        "name": "agentanbud",
        "version": version_string(),
        "build": build_info(),
        "description": "Swedish public procurement — open data for AI agents. REST + MCP at https://www.agentanbud.se",
        "transport": "streamable-http",
        "endpoint": "/mcp",
        "mcp_version": "2024-11-05",
        "tools_count": len(_tool_list()),
        "instructions": "POST JSON-RPC 2.0 to /mcp. Method 'tools/list' lists tools. Method 'tools/call' invokes one.",
        "client_config": {
            "mcpServers": {
                "agentanbud": {
                    "url": "https://www.agentanbud.se/mcp",
                    "transport": "streamable-http"
                }
            }
        }
    }, headers=_cors_headers())


@mcp_router.post("/mcp")
async def mcp_post(request: Request):
    """Handle JSON-RPC 2.0 MCP requests.

    Per MCP spec, the request body is a single JSON-RPC object (not batched).
    Responses follow JSON-RPC 2.0:
      - Success: { jsonrpc, id, result }
      - Error:   { jsonrpc, id, error: { code, message, data? } }
    """
    _evict_expired_sessions()

    # Get / create session
    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        session_id = str(uuid.uuid4())
        SESSIONS[session_id] = {"created": time.time(), "last_seen": time.time()}
    else:
        if session_id not in SESSIONS:
            SESSIONS[session_id] = {"created": time.time(), "last_seen": time.time()}
        SESSIONS[session_id]["last_seen"] = time.time()

    # Parse body
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}},
            status_code=400,
            headers={**_cors_headers(), "mcp-session-id": session_id}
        )

    method = body.get("method")
    rpc_id = body.get("id")
    params = body.get("params") or {}

    # Route
    try:
        if method == "initialize":
            # Every MCP client identifies itself here. We were discarding it,
            # which meant we could count agent sessions but not say what they
            # were — Claude Code, Cursor, Codex, Cline, a homegrown script.
            # That is the interesting question about agent adoption, so record
            # it: client name + version only, no per-user identifier.
            _log_connect(params.get("clientInfo") or {},
                         params.get("protocolVersion"), session_id)
            result = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "agentanbud", "version": version_string()},
                "capabilities": {"tools": {"listChanged": False}}
            }
        elif method == "notifications/initialized":
            # Client→server notification, no response needed (but we still ack with 204)
            return Response(status_code=204, headers={**_cors_headers(), "mcp-session-id": session_id})
        elif method == "ping":
            result = {}  # MCP keepalive
        elif method == "tools/list":
            result = {"tools": _tool_list(_is_authed(request))}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not name:
                raise ValueError("Missing 'name' in tools/call params")
            if name in WRITE_TOOLS and not _is_authed(request):
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": rpc_id,
                     "error": {"code": -32001,
                               "message": f"Tool '{name}' requires the X-Admin-Key header."}},
                    status_code=401,
                    headers={**_cors_headers(), "mcp-session-id": session_id},
                )
            content = await _dispatch_tool(name, arguments, session_id)
            result = _format_result(content)
        else:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rpc_id,
                 "error": {"code": -32601, "message": f"Method not found: {method}"}},
                status_code=404,
                headers={**_cors_headers(), "mcp-session-id": session_id}
            )
    except Exception as exc:
        LOG.exception("MCP error")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id,
             "error": {"code": -32603, "message": f"Internal error: {type(exc).__name__}: {exc}"}},
            status_code=500,
            headers={**_cors_headers(), "mcp-session-id": session_id}
        )

    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "result": result},
        headers={**_cors_headers(), "mcp-session-id": session_id}
    )


@mcp_router.delete("/mcp")
async def mcp_delete(request: Request):
    """End a session (MCP Streamable HTTP)."""
    session_id = request.headers.get("mcp-session-id")
    if session_id and session_id in SESSIONS:
        SESSIONS.pop(session_id, None)
    return Response(status_code=204, headers=_cors_headers())
