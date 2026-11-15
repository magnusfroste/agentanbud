#!/usr/bin/env python3
"""When search_tenders finds nothing open, the answer says what does exist
and what to do: closed and awarded matches, other words that find open
tenders, expected renewals in the field, and the feed. And words people
search for that the notices write differently (chatbot, nödström) find the
tenders. Offline.

Run: python3 scripts/test_no_hits.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


DB = tempfile.mktemp(suffix=".db")
m.create_app(DB)
conn = connect(DB)
soon = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
for src, sid, title, desc, dl, cpv in [
        ("ted_awards", "1", "Reservkraftsystem", "Tilldelat.", None, ["31120000"]),
        ("mercell", "2", "Reservkraftverk till vattenverk", "Nytt aggregat.", soon, ["31120000"]),
        ("mercell", "3", "Chattfunktion för medborgarservice", "Svar dygnet runt.", soon, ["72000000"]),
]:
    upsert_tender(conn, {"source_system": src, "source_id": sid, "tender_url": "https://example.invalid",
                         "title": title, "authority": "Kinda kommun", "cpv_codes": cpv, "deadline": dl,
                         "published_at": "2026-10-01", "description": desc, "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})
conn.commit()


def search(**args):
    return asyncio.run(mcp_server._search_tenders_uncached(conn, args))[0].text


t = search(query="reservkraftsystem")
check("no open hits: said plainly, not as a Python dict",
      t.startswith('Inga öppna upphandlingar matchar "reservkraftsystem"') and "{" not in t, t)
check("…counts the closed and awarded ones and how to see them",
      "1 avslutade eller tilldelade" in t and "open_only=false" in t and "get_winner_history" in t, t)
check("…points to expected renewals in the same field", "expected_tenders(cpv='311')" in t, t)
check("…suggests a word that does find open tenders, with the count", '"reservkraft' in t and "(1)" in t, t)
check("…and gives the feed for the search",
      "https://www.agentanbud.se/feed.xml?q=reservkraftsystem" in t, t)

t = search(query="qwertyzz")
check("nothing at all: still a clean answer with the feed",
      t.startswith('Inga öppna upphandlingar matchar "qwertyzz"') and "avslutade" not in t
      and "feed.xml?q=qwertyzz" in t, t)

t = search(query="chatbot")
check("chatbot finds the chattfunktion", "Chattfunktion för medborgarservice" in t, t[:200])
t = search(query="nödström")
check("nödström finds reservkraft", "Reservkraftverk till vattenverk" in t, t[:200])

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
