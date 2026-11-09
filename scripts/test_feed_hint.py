#!/usr/bin/env python3
"""An MCP agent is told that RSS exists: in the handshake, in the
search_tenders description, at the end of every search (with the feed URL for
that exact search), and in the pause message. The URL it is given answers.
Offline.

Run: python3 scripts/test_feed_hint.py
"""
import os
import pathlib
import sys
import tempfile
from urllib.parse import urlsplit

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
import mcp_shared  # noqa: E402
from app.conduct import Paused  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
mcp_server.DB_PATH = DB
c = TestClient(APP, base_url="https://testserver", headers={"cf-connecting-ip": "198.51.100.31"})
H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


init = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                                  "clientInfo": {"name": "t", "version": "1"}}}).json()
check("the handshake carries instructions about the feed",
      "feed.xml" in (init["result"].get("instructions") or ""), str(init["result"]))
check("…and so does the GET description of /mcp", "feed.xml" in c.get("/mcp").json()["instructions"])

tools = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
desc = next(t["description"] for t in tools["result"]["tools"] if t["name"] == "search_tenders")
check("search_tenders' description says to subscribe", "feed.xml" in desc and "polling" in desc)
stdio = next(t.description for t in mcp_server._tool_objects() if t.name == "search_tenders")
check("…in the stdio server too", "feed.xml" in stdio)

for i, args in enumerate(({"query": "ventilation", "cpv": "45", "authority": "Malmö stad"},
                          {"query": "ventilation"}, {"query": "ventilation"}), start=3):
    r = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                        "params": {"name": "search_tenders", "arguments": args}}).json()
    last = r["result"]["content"][-2]["text"]  # the freshness line stays last
    want = mcp_shared.feed_url(args["query"], args.get("cpv", ""), args.get("authority", ""))
    check(f"the search carries its own feed URL ({i - 2})", want in last, last)

url = mcp_shared.feed_url("ventilation", "45", "Malmö stad")
check("the feed URL keeps all three filters",
      "q=ventilation" in url and "cpv=45" in url and "authority=Malm" in url, url)
part = urlsplit(url)
r = c.get(part.path + "?" + part.query)
check("the feed URL answers with RSS", r.status_code == 200 and "<rss" in r.text, str(r.status_code))
check("an empty search gets the plain feed", mcp_shared.feed_url() == "https://www.agentanbud.se/feed.xml")

check("the pause message points to the feed", "feed.xml" in Paused(60, 1500, None).message())

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
