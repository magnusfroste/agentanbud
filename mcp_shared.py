"""
Values shared by both MCP transports.

mcp_server.py (stdio) and mcp_http.py (Streamable HTTP) each declare their own
tool schemas. That's fine for prose, but enum values are facts about the data
and had already drifted: the stdio server advertised source
["mercell", "ted"] while the HTTP server advertised all five, so an agent got
a different answer depending on how it connected. Anything enumerable that
both must agree on belongs here.
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
