#!/usr/bin/env python3
"""search_tenders answers come from a five-minute cache for identical
arguments, every call is still logged, and the cache is bounded. Offline.

Run: python3 scripts/test_search_cache.py
"""
import os
import pathlib
import sys
import tempfile

KEY = "k" * 64
os.environ["ADMIN_API_KEY"] = KEY
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app.db import connect  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
mcp_server.DB_PATH = DB
c = TestClient(APP, base_url="https://testserver", headers={"cf-connecting-ip": "198.51.100.30"})
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


def call(args, i):
    r = c.post("/mcp", headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
               json={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                     "params": {"name": "search_tenders", "arguments": args}})
    return r.json()["result"]


cache = mcp_server.SEARCH_CACHE
h0, m0 = cache.hits, cache.misses
a = call({"query": "ventilation", "limit": 5}, 1)
b = call({"query": "ventilation", "limit": 5}, 2)
check("the first search is done for real", cache.misses == m0 + 1, f"{cache.misses - m0}")
check("the same search again comes from the cache", cache.hits == h0 + 1, f"{cache.hits - h0}")
check("…with the same answer", a == b)
call({"limit": 5, "query": "ventilation"}, 3)
check("argument order does not matter", cache.hits == h0 + 2)
call({"query": "ventilation", "limit": 6}, 4)
check("different arguments are a different search", cache.misses == m0 + 2)

conn = connect(DB)
try:
    n = conn.execute("SELECT COUNT(*) FROM usage_log WHERE action = 'tool:search_tenders'").fetchone()[0]
finally:
    conn.close()
check("every call is still logged, cached or not", n == 4, f"{n}")

k = mcp_server.SearchCache.key({"query": "x"})
cache.put(k, ["answer"], now=1000.0)
check("an answer is served within five minutes", cache.get(k, now=1000.0 + 299) == ["answer"])
check("…and not after", cache.get(k, now=1000.0 + 301) is None)

small = mcp_server.SearchCache()
small.MAX_ENTRIES = 3
for i in range(5):
    small.put(f"k{i}", [i], now=1.0)
check("the cache is bounded, oldest out first", small.stats()["entries"] == 3 and small.get("k0", 1.0) is None and small.get("k4", 1.0) == [4])

c.post("/ops/login", data={"key": KEY}, headers={"origin": "https://testserver"}, follow_redirects=False)
page = c.get("/ops/agenter").text
check("the Agents page shows the cache", "Sökcache" in page and "ur cachen" in page.lower())
st = c.get("/api/ops").json()["agenter"]["search_cache"]
check("/api/ops reports hits and misses", st["hits"] >= 2 and st["misses"] >= 2, f"{st}")

print()
if failures:
    print(f"FAIL — {failures} case(s)")
    sys.exit(1)
print("PASS")
