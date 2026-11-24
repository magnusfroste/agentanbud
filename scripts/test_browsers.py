#!/usr/bin/env python3
"""Programs with a browser's user agent are crawlers, not people.

In November 2026 a crawler sending an ordinary browser user agent from a new
address for every page was counted as about a thousand people a day. Every
page now reports back with an empty POST to /api/seen when its script runs
(app/browsers.py); a pageview whose visitor never did is moved to the
crawler counts. These cases check that, and what must not change: views
from before the first report, feeds, real browsers, and the totals.

Run: python3 scripts/test_browsers.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import browsers  # noqa: E402
from app.db import connect, prune_logs  # noqa: E402
from app.insights import format_usage_markdown, usage_summary  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


DB = tempfile.mktemp(suffix=".db")
c = TestClient(m.create_app(DB), base_url="https://testserver")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"


def view(path, ip, ua=UA):
    return c.get(path, headers={"user-agent": ua, "cf-connecting-ip": ip})


def human_views():
    conn = connect(DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM usage_log WHERE action = 'view' "
                            "AND json_extract(meta, '$.bot') = 0").fetchone()[0]
    finally:
        conn.close()


def age_views(minutes):
    conn = connect(DB)
    conn.execute("UPDATE usage_log SET created_at = ? WHERE action = 'view'",
                 ((datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    conn.close()


def reclassify():
    conn = connect(DB)
    try:
        return browsers.reclassify(conn)
    finally:
        conn.close()


print("Every page carries the script")
page = view("/", "10.0.0.9").text
check("the start page calls /api/seen", "/api/seen" in page)
check("…and so does a page from another template", "/api/seen" in view("/agenter", "10.0.0.9").text)

print("\nBefore any browser has reported, nothing changes")
age_views(30)
check("no reclassification without a first report", reclassify() == 0)

print("\nThe report")
r = c.post("/api/seen", headers={"user-agent": UA, "cf-connecting-ip": "10.0.0.1"})
check("/api/seen answers 204 with nothing", r.status_code == 204 and not r.content, str(r.status_code))
c.post("/api/seen", headers={"user-agent": "Googlebot/2.1", "cf-connecting-ip": "10.0.0.5"})
c.post("/api/seen", headers={"user-agent": UA})
conn = connect(DB)
marked = conn.execute("SELECT COUNT(*) FROM browser_js").fetchone()[0]
conn.close()
check("one mark: not for a named bot, not without an address", marked == 1, str(marked))

print("\nAfter the first report")
for p in ("/", "/agenter", "/kopare"):
    view(p, "10.0.0.1")                         # a browser that ran the script
for i in range(5):
    view("/agenter", f"10.1.0.{i}")             # a new address per page, no script
view("/feed.xml", "10.2.0.1")                   # a feed reader
fresh = human_views()
check("nothing is moved while the script still has time to report", reclassify() == 0)
check("…nor the views from before the first report", human_views() == fresh)

later = datetime.now(timezone.utc) + timedelta(minutes=20)
conn = connect(DB)
moved = browsers.reclassify(conn, now=later)
conn.close()
check("the five scriptless views become crawler views", moved == 5, str(moved))
check("…the browser's three views and the feed stay human", human_views() == fresh - 5,
      f"{human_views()} vs {fresh - 5}")
conn = connect(DB)
check("…and running it again moves nothing", browsers.reclassify(conn, now=later) == 0)
conn.close()

conn = connect(DB)
counters = dict(conn.execute("SELECT kind, SUM(n) FROM daily_counters GROUP BY kind").fetchall())
s = usage_summary(conn, days=1)
conn.close()
check("they are counted as crawler views, by page type, and as scriptless",
      counters.get("bot_view") == 5 and counters.get("bot_view:agenter") == 5 and counters.get("nojs_view") == 5,
      str(counters))
check("the summary shows them, and the total is unchanged",
      s["visits"]["no_js"] == 5 and s["visits"]["bot"] >= 5 and s["visits"]["total"] == s["visits"]["human"] + s["visits"]["bot"],
      str(s["visits"]))
check("…and says so in words", "inte kör JavaScript" in format_usage_markdown(s))
page = c.get("/analytics", headers={"user-agent": "curl/8"}).text
check("/analytics shows the figure", "inte kör JavaScript" in page)

print("\nRetention")
conn = connect(DB)
conn.execute("INSERT INTO browser_js (day, v, first_at) VALUES ('2000-01-01', 'old', '2000-01-01 00:00:00')")
conn.commit()
prune_logs(conn)
left = conn.execute("SELECT COUNT(*) FROM browser_js WHERE day = '2000-01-01'").fetchone()[0]
conn.close()
check("marks are pruned with the usage log", left == 0)

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
