#!/usr/bin/env python3
"""The growth page: each day summarised once and kept after usage_log forgets
it, weeks from Monday to Sunday, visitors averaged per day (ids change daily),
agent clients as a union over the week. Offline.

Run: python3 scripts/test_growth.py
"""
import json
import os
import pathlib
import sys
import tempfile

KEY = "k" * 64
os.environ["ADMIN_API_KEY"] = KEY
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import ops  # noqa: E402
from app.db import connect  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
TODAY = "2026-10-15"            # a Thursday; its week starts Monday 10-12
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


def row(conn, day, channel, action, meta, query=None):
    conn.execute("INSERT INTO usage_log (channel, action, query, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                 (channel, action, query, json.dumps(meta), f"{day} 12:00:00"))


conn = connect(DB)
try:
    # Week of 09-28: before visitor ids existed, two human views and nothing else.
    row(conn, "2026-10-01", "browser", "view", {"bot": 0})
    row(conn, "2026-10-01", "browser", "view", {"bot": 0})
    # Week of 10-05: two days with ids.
    for v in ("a", "b", "a"):
        row(conn, "2026-10-07", "browser", "view", {"bot": 0, "v": v})
    row(conn, "2026-10-07", "browser", "view", {"bot": 1})                 # a crawler: not a human view
    row(conn, "2026-10-07", "browser", "search", {"bot": 0, "v": "a"}, "klimat")
    row(conn, "2026-10-07", "api", "search", {"bot": 0}, "vvs")
    row(conn, "2026-10-07", "api", "search", {"bot": 0}, "vvs")
    row(conn, "2026-10-07", "mcp", "mcp:connect", {"client": "claude-code", "_sid": "s1"})
    row(conn, "2026-10-07", "mcp", "tool:search_tenders", {"_sid": "s1"}, "el")
    for v in ("c", "d"):
        row(conn, "2026-10-08", "browser", "view", {"bot": 0, "v": v})
    # This week: 10-14 three visitors, two clients; today one visitor.
    for v in ("e", "f", "g"):
        row(conn, "2026-10-14", "browser", "view", {"bot": 0, "v": v})
    row(conn, "2026-10-14", "mcp", "mcp:connect", {"client": "codex-mcp-client", "_sid": "s2"})
    row(conn, "2026-10-14", "mcp", "mcp:connect", {"client": "claude-code", "_sid": "s3"})
    row(conn, "2026-10-14", "mcp", "mcp:connect", {"client": "smoke", "_sid": "s4"})   # our own monitoring
    row(conn, TODAY, "browser", "view", {"bot": 0, "v": "h"})
    conn.commit()

    print("Summarising days")
    n = ops.rollup_daily(conn, today=TODAY)
    days = {r["day"]: dict(r) for r in conn.execute("SELECT * FROM daily_usage")}
    check("every finished day is summarised", set(days) == {"2026-10-01", "2026-10-07", "2026-10-08", "2026-10-14"}, f"{sorted(days)}")
    check("today is not stored; it is still running", TODAY not in days)
    d7 = days["2026-10-07"]
    check("a day's figures are right",
          (d7["visitors"], d7["human_views"], d7["web_searches"], d7["api_searches"], d7["agent_calls"]) == (2, 3, 1, 2, 1), f"{d7}")
    check("…and its clients are named", json.loads(d7["agent_clients"]) == ["claude-code"], d7["agent_clients"])
    check("a day before visitor ids reads 0 visitors", days["2026-10-01"]["visitors"] == 0)
    ops.rollup_daily(conn, today=TODAY)
    check("running it again changes nothing", conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0] == 4)

    print("Days outlive usage_log")
    conn.execute("DELETE FROM usage_log WHERE created_at < '2026-10-10'")    # as prune_logs would
    conn.commit()
    ops.rollup_daily(conn, today=TODAY)
    kept = conn.execute("SELECT human_views FROM daily_usage WHERE day = '2026-10-07'").fetchone()
    check("a day pruned from usage_log keeps its summary", kept and kept[0] == 3, f"{kept}")

    print("Weeks")
    W = ops.weekly(conn, 3, today=TODAY)
    check("three weeks, oldest first, Monday to Sunday", [w["monday"] for w in W] == ["2026-09-28", "2026-10-05", "2026-10-12"], f"{[w['monday'] for w in W]}")
    check("the current week is marked as partial", W[-1]["partial"] and not W[-2]["partial"])
    check("visitors are averaged per day, today included", W[-1]["visitors_per_day"] == 2, f"{W[-1]['visitors_per_day']}")
    check("last week's average is (2 + 2) / 2", W[-2]["visitors_per_day"] == 2, f"{W[-2]['visitors_per_day']}")
    check("a week without ids has no visitor figure, not zero", W[0]["visitors_per_day"] is None)
    check("agent clients are a union over the week, without our own monitoring", W[-1]["agent_clients"] == 2 and W[-2]["agent_clients"] == 1,
          f"{W[-1]['agent_client_names']} / {W[-2]['agent_client_names']}")
    check("human views are summed over the week", (W[0]["human_views"], W[-2]["human_views"]) == (2, 5), f"{W[0]['human_views']}, {W[-2]['human_views']}")
    check("change compares the last two complete weeks", ops.growth_change(W, "human_views") == 150, f"{ops.growth_change(W, 'human_views')}")
    check("no change without two weeks to compare", ops.growth_change(W, "visitors_per_day") is None)
finally:
    conn.close()

print("The page")
c = TestClient(APP, base_url="https://testserver", headers={"cf-connecting-ip": "198.51.100.20"})
check("/ops/tillvaxt needs a login", c.get("/ops/tillvaxt", follow_redirects=False).status_code == 303)
c.post("/ops/login", data={"key": KEY}, headers={"origin": "https://testserver"}, follow_redirects=False)
r = c.get("/ops/tillvaxt")
check("/ops/tillvaxt renders behind login", r.status_code == 200 and "Unika besökare per dag" in r.text and "Veckor" in r.text, f"HTTP {r.status_code}")
check("the side panel links it", 'href="/ops/tillvaxt"' in c.get("/ops").text)
check("the overview has the growth card", "Tillväxt, denna vecka" in c.get("/ops").text)
check("/api/ops carries the weeks", "weeks" in c.get("/api/ops").json().get("tillvaxt", {}))

print()
if failures:
    print(f"FAIL — {failures} case(s)")
    sys.exit(1)
print("PASS")
