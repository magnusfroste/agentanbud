#!/usr/bin/env python3
"""Feeds without an account: RSS for new tenders, iCal for their deadlines.

Every alert service we found needs an account and sends email. Any search,
industry, county or buyer page here has a feed.xml and a kalender.ics
instead, for a reader, a calendar or an agent to poll. These cases check
what each feed holds (the page's own open tenders), that both formats are
well formed, and that the pages link them.

Run: python3 scripts/test_feeds.py
"""
import os
import pathlib
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import related  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
conn = connect(DB)
n = [0]


def add(title, authority="Göteborgs Stad", days=10, cpv="45331000", region="Västra Götalands län",
        published="2026-10-01", source="mercell", deadline=None):
    n[0] += 1
    upsert_tender(conn, {"source_system": source, "source_id": str(n[0]), "tender_url": "https://example.invalid",
                         "title": title, "authority": authority, "cpv_codes": [cpv],
                         "deadline": deadline or (iso(days) if days is not None else None), "published_at": published,
                         "description": "Byte av aggregat, <b>fet</b> & mer", "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": region, "raw_json": "{}"})


add("Ventilation skola, etapp 1", published="2026-10-05")
# Computed once: the check below compares it to the second, and computing it
# again there failed whenever the clock passed a second in between.
SPORTHALL = iso(20)
add("Ventilation sporthall", published="2026-10-09", deadline=SPORTHALL)
add("Ventilation stängd", days=-3)
add("Ventilation utan deadline", days=None, published="2026-10-07")
add("Tilldelning ventilation", source="ted_awards")
for i in range(4):
    add(f"IT-drift {i}", cpv="72000000", region="Stockholms län", authority="Region Stockholm")
add("Snöröjning", cpv="90620000")
conn.commit()
related.reset()
c = TestClient(APP, base_url="https://testserver")

print("RSS för en sökning")
r = c.get("/feed.xml", params={"q": "ventilation"})
check("RSS 2.0 med rätt typ", r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml"))
root = ET.fromstring(r.content)
titles = [i.findtext("title") for i in root.iter("item")]
check("öppna, nyaste först, utan stängda och tilldelningar",
      titles == ["Ventilation sporthall", "Ventilation utan deadline", "Ventilation skola, etapp 1"], str(titles))
item = next(root.iter("item"))
check("länk, guid och datum", item.findtext("link").endswith("/tenders/2") and item.findtext("guid") == item.findtext("link")
      and item.findtext("pubDate").startswith("Fri, 09 Oct 2026"), item.findtext("pubDate"))
check("beskrivningen: köpare, sista dag, region — och HTML escapad", "Göteborgs Stad · Sista anbudsdag" in item.findtext("description")
      and "<b>fet</b> & mer" in item.findtext("description") and "&lt;b&gt;" in r.text)
check("kanalens titel och länk", root.findtext("channel/title") == "Agentanbud: ventilation"
      and root.findtext("channel/link").endswith("/browse?q=ventilation"))
check("utan sökning: alla öppna", len(list(ET.fromstring(c.get("/feed.xml").content).iter("item"))) == 8)
check("CPV-filter", len(list(ET.fromstring(c.get("/feed.xml", params={"cpv": "72"}).content).iter("item"))) == 4)

print("\nKalender")
r = c.get("/kalender.ics", params={"q": "ventilation"})
body = r.text
check("text/calendar, CRLF", r.headers["content-type"].startswith("text/calendar") and "\r\n" in body
      and body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n"))
check("bara upphandlingar med sista dag", body.count("BEGIN:VEVENT") == 2, str(body.count("BEGIN:VEVENT")))
check("händelsen slutar vid sista anbudsdag, påminnelse tre dagar före",
      "DTEND:" + SPORTHALL.replace("-", "").replace(":", "") in body and "TRIGGER:-P3D" in body)
check("kommatecken escapade, inga rader över 75 byte",
      "Ventilation skola\\, etapp 1" in body and all(len(x.encode()) <= 75 for x in body.split("\r\n")))
check("stabila UID:n", "UID:tender-1@agentanbud.se" in body)

print("\nBransch, län och köpare")
for path, want in (("/upphandlingar/bransch/it/feed.xml", 4), ("/upphandlingar/lan/stockholm/feed.xml", 4),
                   ("/kopare/goteborgs-stad/feed.xml", 3)):
    r = c.get(path)
    got = len(list(ET.fromstring(r.content).iter("item"))) if r.status_code == 200 else -1
    check(f"{path}: {want}", got == want, str(got))
check("köparens kalender", c.get("/kopare/goteborgs-stad/kalender.ics").text.count("BEGIN:VEVENT") == 3)
check("okända: 404", c.get("/kopare/finns-inte/feed.xml").status_code == 404
      and c.get("/upphandlingar/bransch/finns-inte/feed.xml").status_code == 404
      and c.get("/kopare/goteborgs-stad/annat.txt").status_code == 404)

print("\nSidorna länkar flödena")
h = c.get("/browse", params={"q": "ventilation"}).text
check("sökningen", 'href="/feed.xml?q=ventilation"' in h and 'href="/kalender.ics?q=ventilation"' in h
      and 'rel="alternate" type="application/rss+xml"' in h)
check("branschsidan", 'href="/upphandlingar/bransch/it/feed.xml"' in c.get("/upphandlingar/bransch/it").text)
check("köparsidan", 'href="/kopare/goteborgs-stad/kalender.ics"' in c.get("/kopare/goteborgs-stad").text)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
