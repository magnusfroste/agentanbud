#!/usr/bin/env python3
"""Unique visitors without storing who they are, and search terms counted per
searcher. Offline: a temporary database and FastAPI's test client.

Run: python3 scripts/test_visitors.py
"""
import json
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import visitors  # noqa: E402
from app.db import connect, log_usage  # noqa: E402
from app.insights import usage_summary  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


IP_A, IP_B, UA = "203.0.113.7", "203.0.113.8", "Mozilla/5.0 test"

print("The visitor id")
conn = connect(DB)
try:
    a1 = visitors.visitor_id(conn, IP_A, UA, day="2026-10-01")
    a2 = visitors.visitor_id(conn, IP_A, UA, day="2026-10-01")
    b1 = visitors.visitor_id(conn, IP_B, UA, day="2026-10-01")
    check("the same visitor on the same day gets the same id", a1 == a2)
    check("another address gets another id", a1 != b1)
    check("no address, no id", visitors.visitor_id(conn, None, UA) is None)
    a_next = visitors.visitor_id(conn, IP_A, UA, day="2026-10-02")
    check("the next day the same visitor gets a new id", a_next != a1)
    days = [r[0] for r in conn.execute("SELECT day FROM visitor_salt")]
    check("yesterday's salt is gone once today's exists", days == ["2026-10-02"], f"{days}")
finally:
    conn.close()

print("Pageviews")
c = TestClient(APP, base_url="https://testserver",
               headers={"cf-connecting-ip": IP_A, "user-agent": UA})
check("a page answers", c.get("/system").status_code == 200)
c.get("/system")
conn = connect(DB)
try:
    rows = conn.execute("SELECT meta FROM usage_log WHERE action = 'view'").fetchall()
    metas = [json.loads(r[0] or "{}") for r in rows]
    check("the view carries a visitor id", metas and all(x.get("v") for x in metas), f"{metas}")
    check("two views by one visitor share it", len({x.get("v") for x in metas}) == 1)
    dump = "\n".join(str(tuple(r)) for t in ("usage_log", "visitor_salt", "daily_counters")
                     for r in conn.execute(f"SELECT * FROM {t}"))
    check("the raw address is stored nowhere", IP_A not in dump)
finally:
    conn.close()
c2 = TestClient(APP, base_url="https://testserver", headers={"user-agent": UA})
c2.get("/system")
check("without the header the view is still logged, as unknown",
      json.loads(connect(DB).execute(
          "SELECT meta FROM usage_log WHERE action = 'view' ORDER BY id DESC LIMIT 1").fetchone()[0]).get("v") is None)
check("…and /ops can see the header went missing", visitors.HEADER["missing"] >= 1)

print("Search terms, counted per searcher")
conn = connect(DB)
try:
    # One automated client, 100 calls, a new MCP session each time, one visitor.
    for i in range(100):
        log_usage(conn, "mcp", "tool:search_tenders", query="tjänstedesign",
                  meta={"_sid": f"s{i:03d}", "v": "loopvisitor1"})
    # Three different people, one search each.
    for v in ("p1", "p2", "p3"):
        log_usage(conn, "browser", "search", query="vinddata", meta={"bot": 0, "v": v})
    # The old shape: no visitor id, a session per call, the client named on connect.
    for i in range(50):
        sid = f"old{i:03d}"
        log_usage(conn, "mcp", "mcp:connect", meta={"_sid": sid, "client": "loop-client"})
        log_usage(conn, "mcp", "tool:search_tenders", query="varumärkesstrategi", meta={"_sid": sid})
    # Sessionless calls: no handshake, so no connect event and no client name.
    for i in range(40):
        log_usage(conn, "mcp", "tool:search_tenders", query="strategisk kommunikation",
                  meta={"_sid": f"nohs{i:03d}"})
    # Old REST API rows: no id, no session. A script looping one term.
    for i in range(60):
        log_usage(conn, "api", "search", query="ventilation", meta={"bot": 0})
    # Old web rows: people typing, still counted per search.
    for i in range(2):
        log_usage(conn, "browser", "search", query="solceller", meta={"bot": 0})
    conn.commit()
    s = usage_summary(conn)
finally:
    conn.close()
terms = {t["term"]: t["n"] for t in s["top_terms"]}
check("a hundred calls by one visitor count once", terms.get("tjänstedesign") == 1, f"{terms}")
check("three people count three times", terms.get("vinddata") == 3, f"{terms}")
check("old rows without an id count once per client and day", terms.get("varumärkesstrategi") == 1, f"{terms}")
check("sessionless calls without an id count once per day", terms.get("strategisk kommunikation") == 1, f"{terms}")
# 50 old sessions each made a real handshake and count as sessions; the 100
# calls by one visitor count once, and the 40 sessionless calls once per day.
check("agents are counted per visitor, not per call: 50 + 1 + 1", s["agent_sessions"] == 52, f"{s['agent_sessions']}")
check("old API rows without an id count once per day", terms.get("ventilation") == 1, f"{terms}")
check("old web searches still count per search", terms.get("solceller") == 2, f"{terms}")
check("so the people outrank the loop", s["top_terms"][0]["term"] == "vinddata", f"{s['top_terms'][:3]}")

print("Unique visitors per day")
check("today's unique visitors are counted", s["visitors"]["today"] == 1, f"{s['visitors']}")
check("days before the ids began do not drag the average down",
      s["visitors"]["avg_7d"] == 1 and s["visitors"]["measured_days"] == 1, f"{s['visitors']}")

print()
if failures:
    print(f"FAIL — {failures} case(s)")
    sys.exit(1)
print("PASS")
