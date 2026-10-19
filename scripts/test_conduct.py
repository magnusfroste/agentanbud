#!/usr/bin/env python3
"""A caller that behaves like a loop is told so, paused only at an absurd
rate, and counted as automation rather than as agents. Offline.

Run: python3 scripts/test_conduct.py
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
from app import conduct  # noqa: E402
from app.conduct import Entry, Ledger, Paused, check_pause, classify, guidance  # noqa: E402
from app.db import connect, log_usage, upsert_tender  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
mcp_server.DB_PATH = DB

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


# ---- 1. classify(): the one function both the live ledger and the log use --
print("1. classify")
check("one call is 'single'", classify(1, 1, 1, 1) == "single")
check("several calls, several questions → explorer", classify(12, 8, 1, 3) == "explorer")
check("several calls, one question, few repeats → monitor", classify(4, 1, 1, 4) == "monitor")
check("new session per call at scale → loop",
      classify(100, 50, 95, 2) == "loop", "even with 50 distinct queries")
check("same question 20x since data changed → loop", classify(25, 1, 1, 25) == "loop")
check("the October loop: 1 call/session, 139 terms, 20k sessions → loop",
      classify(19782, 139, 19782, 2193) == "loop")
check("Codex in September: 65 calls, 20 terms, 3 sessions → explorer",
      classify(65, 20, 3, 5) == "explorer")

# ---- 2. the ledger: one caller, one day, resets with the data -------------
print("\n2. ledger")
L = Ledger()
e = None
for i in range(6):
    e = L.record("v1", f"sid{i}", "tjänstedesign", "2026-10-19T06:08:00Z", now=1000.0 + i)
check("six calls recorded", e.calls == 6)
check("six distinct sessions", len(e.sessions) == 6)
check("same query counted six times", e.queries["tjänstedesign"] == 6)
e = L.record("v1", "sid9", "tjänstedesign", "2026-10-20T06:08:00Z", now=2000.0)
check("query counts reset when the data moves", e.queries["tjänstedesign"] == 1,
      f"got {e.queries['tjänstedesign']}")
check("calls do not reset with the data", e.calls == 7)
check("no visitor id → no entry, nothing to say",
      L.record(None, "sid", "q", "x") is None and guidance(None) == [])

# ---- 3. guidance(): escalates, names the fix, never for a good agent -----
print("\n3. guidance")
good = Entry(calls=5, sessions={"a"}, queries={"vattenmätare": 1, "scada": 1, "iot": 1, "lorawan": 1, "elmätare": 1},
             last_query="elmätare")
check("an explorer gets no guidance", guidance(good, now=0) == [])

rep = Entry(calls=5, sessions={"a"}, queries={"it": 5}, last_query="it", data_mark="2026-10-19T06:08:00Z")
g = guidance(rep, now=0)
check("5th identical question → repeat note", len(g) == 1 and "5 gånger" in g[0])
check("…which names when the data changed", "2026-10-19 06:08 UTC" in g[0])
check("…and the fix", "Spara svaret" in g[0])

churn = Entry(calls=25, sessions={f"s{i}" for i in range(24)}, queries={f"q{i}": 1 for i in range(25)},
              last_query="q24")
g = guidance(churn, now=0)
check("24 sessions on 25 calls → session-churn note", any("Mcp-Session-Id" in x for x in g))
check("…but not the repeat note (queries differ)", not any("gånger" in x for x in g))

hot = Entry(calls=400, sessions={"a"}, queries={f"q{i}": 1 for i in range(400)}, last_query="q1")
for i in range(conduct.HOT_PER_HOUR):
    hot.recent.append(5000.0 - i)
g = guidance(hot, now=5000.0)
check(f"{conduct.HOT_PER_HOUR} calls in an hour → hot note", any("senaste timmen" in x for x in g))
cold = Entry(calls=400, sessions={"a"}, queries={f"q{i}": 1 for i in range(400)}, last_query="q1")
for i in range(conduct.HOT_PER_HOUR):
    cold.recent.append(5000.0 - 3700 - i)        # all older than an hour
check("same calls an hour ago → no hot note", guidance(cold, now=5000.0) == [])

# ---- 4. pause: only past PAUSE_PER_HOUR, with a retry time ---------------
print("\n4. pause")
try:
    check_pause(hot, "2026-10-20T06:00:00Z", now=5000.0)
    check("hot but under the pause rate is served", True)
except Paused:
    check("hot but under the pause rate is served", False)
flood = Entry(calls=2000, sessions={"a"})
for i in range(conduct.PAUSE_PER_HOUR):
    flood.recent.append(5000.0 - i)
try:
    check_pause(flood, "2026-10-20T06:00:00Z", now=5000.0)
    check("past the pause rate raises", False)
except Paused as p:
    check("past the pause rate raises", True)
    check("…with a retry time", p.retry_after == conduct.PAUSE_SECONDS)
    check("…and the message says when the data changes next", "2026-10-20 06:00 UTC" in p.message())
check("no entry never pauses", check_pause(None, None) is None)

# ---- 5. over HTTP: the note arrives, a flood gets 429 + Retry-After -------
print("\n5. over /mcp")
c = TestClient(APP, base_url="https://testserver", headers={"cf-connecting-ip": "198.51.100.30"})
conn = connect(DB)
upsert_tender(conn, {"source_system": "ted", "source_id": "1", "tender_url": "", "title": "Upphandling IT-drift",
                     "authority": "Testkommun", "cpv_codes": '["72000000"]', "deadline": "2099-01-01",
                     "published_at": "2026-10-01", "description": "", "value": None, "procedure": None,
                     "contract_type": None, "document_type": None, "region": "Sverige", "raw_json": "{}"})
conn.commit()
conn.close()
conduct.LEDGER._entries.clear()


def call(query, sid=None):
    h = {}
    if sid:
        h["mcp-session-id"] = sid
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "search_tenders", "arguments": {"query": query, "limit": 1}}},
               headers=h)
    return r


r = call("it")
check("first call answers 200", r.status_code == 200)
texts = [b["text"] for b in r.json()["result"]["content"]]
check("…with no repeat note yet", not any("gånger" in t for t in texts))
for _ in range(conduct.REPEAT_NOTE_AT - 1):
    r = call("it")
texts = [b["text"] for b in r.json()["result"]["content"]]
check(f"{conduct.REPEAT_NOTE_AT}th identical call carries the repeat note",
      any(f"{conduct.REPEAT_NOTE_AT} gånger" in t for t in texts), texts[-1][:70] if texts else "")
check("…as its own block after the answer, answer unchanged",
      "Upphandling IT-drift" in texts[0] and "gånger" not in texts[0])

# flood: push the ledger past the pause rate directly (a real flood is 1 200 requests)
e = conduct.LEDGER._entries[next(iter(conduct.LEDGER._entries))]
import time as _t
for i in range(conduct.PAUSE_PER_HOUR):
    e.recent.append(_t.time())
r = call("it")
check("a flood is answered 429", r.status_code == 429, f"got {r.status_code}")
check("…with Retry-After", r.headers.get("retry-after") == str(conduct.PAUSE_SECONDS))
body = r.json()
check("…as a JSON-RPC error the agent can read",
      body.get("error", {}).get("code") == -32029 and "Försök igen om" in body["error"]["message"])
check("…carrying the retry seconds as data",
      body["error"]["data"]["retryAfterSeconds"] == conduct.PAUSE_SECONDS)
r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
check("tools/list is never paused — a client may always learn what is here", r.status_code == 200)

# ---- 6. the log: the loop is automated, the explorer is an agent ---------
print("\n6. classify_log + insights")
conn = connect(DB)
conn.execute("DELETE FROM usage_log")
for i in range(30):   # a loop: 30 calls, 30 sessions, same 3 terms, one visitor
    log_usage(conn, "mcp", "tool:search_tenders", query=["a", "b", "c"][i % 3],
              meta={"v": "loopvisitor", "_sid": f"s{i}"})
for i in range(8):    # an explorer: 8 calls, 1 session, 8 terms
    log_usage(conn, "mcp", "tool:search_tenders", query=f"term{i}",
              meta={"v": "agentvisitor", "_sid": "one"})
conn.commit()
kinds = conduct.classify_log(conn)
check("loop caller classified loop", kinds["loop"]["callers"] == 1 and kinds["loop"]["calls"] == 30, str(kinds["loop"]))
check("explorer caller classified explorer", kinds["explorer"]["callers"] == 1 and kinds["explorer"]["calls"] == 8)
from app.insights import usage_summary  # noqa: E402
u = usage_summary(conn)
check("insights: automated_calls = the loop's 30", u["automated_calls"] == 30, str(u["automated_calls"]))
check("insights: agent_calls = the explorer's 8, loop removed", u["agent_calls"] == 8, str(u["agent_calls"]))
check("insights: conduct breakdown present", u["conduct"]["loop"]["calls"] == 30)
conn.close()

r = c.get("/api/analytics?days=30")
check("/api/analytics carries automated_calls", r.status_code == 200 and r.json().get("automated_calls") == 30)
r = c.get("/analytics")
check("/analytics page shows the automated line", r.status_code == 200 and "automatiserade loopar" in r.text)

print()
if failures:
    print(f"❌ {len(failures)} fel:")
    for f in failures:
        print("   •", f)
    sys.exit(1)
print("✅ Alla kontroller passerade")
