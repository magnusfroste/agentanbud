#!/usr/bin/env python3
"""Which pages are read: views per kind of page, people and crawlers.

The buyer, supplier and "väntas" pages were built for search engines and
answer engines, and /analytics could not say whether anyone read them: it
counted views, not which views. Human views carry their path already;
crawler views are counted per day without one. These cases check the
classification, the per-type figures for people (with how many came from a
search engine), the crawler counters per type, and the section on
/analytics. Aggregate only: nothing here identifies a visitor.

Run: python3 scripts/test_page_types.py
"""
import json
import os
import pathlib
import sys
import tempfile

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import usage_cache  # noqa: E402
from app.db import connect  # noqa: E402
from app.insights import format_usage_markdown, page_type, usage_summary  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


print("Sidtyper")
cases = {"/": "start", "/tenders/12": "upphandling", "/browse?q=x": "sok", "/kopare/goteborgs-stad": "kopare",
         "/leverantor/sweco-sverige-ab-5567679849": "leverantor", "/upphandlingar/vantas?bransch=it": "vantas",
         "/upphandlingar/bransch/it": "hub", "/upphandlingar/lan/skane": "hub", "/kopare": "index",
         "/leverantor": "index", "/upphandlingar": "index", "/feed.xml": "flode",
         "/upphandlingar/bransch/it/feed.xml": "flode", "/kopare/x/kalender.ics": "flode", "/blogg/inlagg": "blogg",
         "/agenter": "agenter", "/analytics": "ovrigt"}
bad = {p: page_type(p) for p, want in cases.items() if page_type(p) != want}
check("varje sorts sida känns igen, flöden före sidan de hänger på", not bad, str(bad))

DB = tempfile.mktemp(suffix=".db")
c = TestClient(m.create_app(DB), base_url="https://testserver")
ua = "Mozilla/5.0 (Macintosh) Safari/605"
g = {"user-agent": ua, "referer": "https://www.google.com/"}
c.get("/kopare", headers={**g, "cf-connecting-ip": "10.0.0.1"})
c.get("/leverantor", headers={**g, "cf-connecting-ip": "10.0.0.1"})
c.get("/leverantor", headers={"user-agent": ua, "cf-connecting-ip": "10.0.0.2"})
c.get("/upphandlingar", headers={"user-agent": ua, "referer": "https://www.linkedin.com/", "cf-connecting-ip": "10.0.0.3"})
bot = {"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}
for path in ("/leverantor", "/kopare", "/kopare", "/feed.xml"):
    c.get(path, headers=bot)
conn = connect(DB)
counters = dict(conn.execute("SELECT kind, n FROM daily_counters").fetchall())
check("crawlerbesök räknas totalt och per sidtyp, i samma rad per dygn",
      counters.get("bot_view") == 4 and counters.get("bot_view:index") == 3 and counters.get("bot_view:flode") == 1,
      str(counters))
s = usage_summary(conn, days=1)
idx = next(r for r in s["pages"] if r["type"] == "index")
check("människor per sidtyp: besökare, visningar, från sökmotor",
      idx["views"] == 4 and idx["visitors"] == 3 and idx["from_search"] == 2 and idx["crawler_views"] == 3, str(idx))
check("flöden syns även om bara robotar läste dem",
      any(r["type"] == "flode" and r["views"] == 0 and r["crawler_views"] == 1 for r in s["pages"]), str(s["pages"]))
check("totalen för crawlers är oförändrad", s["visits"]["bot"] == 4, str(s["visits"]))
check("i rapporten till agenter", "Översikter (köpare, leverantörer, branscher): 3 / 4 / 2 / 3" in format_usage_markdown(s))
check("inget om enskilda besökare i svaret", "10.0.0" not in json.dumps(s) and '"v"' not in json.dumps(s["pages"]))
usage_cache._CACHES.pop(DB, None)
h = c.get("/analytics").text
check("analyssidan visar tabellen", "Vilka sidor läses?" in h and "Översikter (köpare, leverantörer, branscher)" in h)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
