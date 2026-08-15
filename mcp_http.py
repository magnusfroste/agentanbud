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

import json
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.db import connect, log_usage
from mcp_tools import tool_dicts
import mcp_server

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
    if name in ("search_tenders",):
        return arguments.get("query")
    if name == "search_knowledge":
        return arguments.get("q")
    if name == "get_authority":
        return arguments.get("name")
    if name == "match_profile":
        keywords = arguments.get("keywords") or []
        return ", ".join(keywords) if keywords else None
    return None


async def _dispatch_tool(name: str, arguments: dict) -> list:
    """Call the right tool handler (reuses the stdio version's logic)."""
    conn = connect(mcp_server.DB_PATH)
    try:
        try:
            log_usage(conn, "mcp", f"tool:{name}", query=_extract_query(name, arguments), meta=arguments or None)
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
        raise ValueError(f"Unknown tool: {name}")
    finally:
        conn.close()


def _tool_list() -> list[dict]:
    """Public tool metadata — shared with the stdio server via mcp_tools.

    `local_only` tools (sync_now) are filtered out: this endpoint is
    read-only by design. Admin actions go through the keyed REST endpoints.
    """
    return tool_dicts(include_local=False)



# ----- FastAPI router -------------------------------------------------------

mcp_router = APIRouter()


def _cors_headers() -> dict:
    """CORS for browser-based MCP clients (Claude.ai, future web tools)."""
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Accept, mcp-session-id",
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
        "version": "0.2.0",
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
            result = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "agentanbud", "version": "0.2.0"},
                "capabilities": {"tools": {"listChanged": False}}
            }
        elif method == "notifications/initialized":
            # Client→server notification, no response needed (but we still ack with 204)
            return Response(status_code=204, headers={**_cors_headers(), "mcp-session-id": session_id})
        elif method == "ping":
            result = {}  # MCP keepalive
        elif method == "tools/list":
            result = {"tools": _tool_list()}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not name:
                raise ValueError("Missing 'name' in tools/call params")
            content = await _dispatch_tool(name, arguments)
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
