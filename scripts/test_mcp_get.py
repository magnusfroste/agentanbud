#!/usr/bin/env python3
"""GET /mcp: server info for a plain GET, 405 for a stream request.

A Streamable HTTP client that GETs /mcp with Accept: text/event-stream must get
either a stream or 405. A 200 with JSON made one client reconnect about eight
times a second for hours. Offline: a temporary database and the test client.

Run: python3 scripts/test_mcp_get.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

c = TestClient(m.create_app(tempfile.mktemp(suffix=".db")))
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


r = c.get("/mcp")
check("a plain GET /mcp still answers with the server info", r.status_code == 200 and r.json().get("name") == "agentanbud",
      f"HTTP {r.status_code}")
for accept in ("text/event-stream", "application/json, text/event-stream", "Text/Event-Stream"):
    r = c.get("/mcp", headers={"accept": accept})
    check(f"a stream request ({accept}) gets 405", r.status_code == 405, f"HTTP {r.status_code}")
    check("…saying which methods do work", "POST" in (r.headers.get("allow") or ""), r.headers.get("allow"))
r = c.post("/mcp", headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
           json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
check("POST /mcp with the same Accept header still works", r.status_code == 200 and "result" in r.json(), f"HTTP {r.status_code}")

print()
if failures:
    print(f"FAIL — {failures} case(s)")
    sys.exit(1)
print("PASS")
