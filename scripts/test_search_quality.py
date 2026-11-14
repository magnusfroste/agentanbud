#!/usr/bin/env python3
"""search_tenders gives no hits that only look like hits. Each case is a
search tried against production on 14 November 2026 that answered with
something unrelated:

  meteorologiska         → SMHI's fruit baskets (the word is in the buyer's name)
  artificiell intelligens → stage lighting (backoff of "intelligens" to "intell")
  NIS 2                  → a bridge over the river Nissan ("nis" as a prefix)
  IT-konsult stockholm   → consultants anywhere ("stockholm" as a word to find)
  Expert AI, Expert AI   → the same procurement twice
  open_only              → award notices, contracts already signed, as open

Run: python3 scripts/test_search_quality.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402
from app.search import split_place  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
mcp_server.DB_PATH = DB
conn = connect(DB)
soon = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
SMHI = "Sveriges meteorologiska och hydrologiska institut"
ROWS = [
    # (source, id, title, authority, description, region, deadline)
    ("mercell", "1", "Fruktkorgar", SMHI, "Leverans av frukt till kontoren.", None, soon),
    ("mercell", "2", "Mätstationer", "Länsstyrelsen", "Meteorologiska mätningar av vind.", None, soon),
    ("mercell", "3", "Ljusriggar till Finnvedsvallen", "Värnamo kommun",
     "Intelligent ljusstyrning för arenan.", None, soon),
    ("mercell", "4", "Renovering av norra bro över Nissan", "Gislaveds kommun", "Brorenovering.", None, soon),
    ("mercell", "5", "Nätinformationssystem (NIS)", "MSB", "Säkerhetskrav enligt NIS 2.", None, soon),
    ("mercell", "6", "IT-konsulter för drift", "Region Stockholm", "Konsulter inom IT.", "Stockholms län", soon),
    ("mercell", "7", "IT-konsulter för utveckling", "Malmö stad", "Konsulter inom IT.", "Skåne län", soon),
    ("ted", "8", "Expert AI", "Försäkringskassan", "AI-tjänster.", "Sverige", soon),
    ("mercell", "9", "Expert AI", "Försäkringskassan", "AI-tjänster.", None, soon),
    ("mercell", "10", "Mailservice", "Postnord", "E-mail och utskick.", None, soon),
    ("ted_awards", "11", "Ventilation sporthall", "Borås kommun", "Tilldelat avtal.", "Sverige", None),
    ("mercell", "12", "Ventilation skola", "Borås kommun", "Byte av aggregat.", None, soon),
]
for src, sid, title, authority, desc, region, dl in ROWS:
    upsert_tender(conn, {"source_system": src, "source_id": sid, "tender_url": "https://example.invalid",
                         "title": title, "authority": authority, "cpv_codes": [], "deadline": dl,
                         "published_at": "2026-10-01", "description": desc, "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": region, "raw_json": "{}"})
conn.commit()


def search(**args):
    out = asyncio.run(mcp_server._search_tenders_uncached(conn, args))
    text = out[0].text
    import re
    return text, re.findall(r"\*\*(.+?)\*\*", text)


text, t = search(query="meteorologiska")
check("meteorologiska: the tender that is about it", t == ["Mätstationer"], str(t))
text, t = search(query="hydrologiska")
check("hydrologiska (only in a buyer's name): the buyer's tenders, said to be so",
      t == ["Fruktkorgar"] and text.startswith("Ingen upphandling nämner"), text[:120])
text, t = search(query="Sveriges meteorologiska")
check("searching for the buyer by name gives the buyer's tenders", t == ["Fruktkorgar"], str(t))

text, t = search(query="artificiell intelligens")
check("artificiell intelligens: no stage lighting", "Ljusriggar till Finnvedsvallen" not in t, str(t))

text, t = search(query="NIS 2")
check("NIS 2: the NIS tender, not the bridge over the Nissan",
      t == ["Nätinformationssystem (NIS)"], str(t))
text, t = search(query="ai")
check("ai: AI, not every word with 'ai' in it", t == ["Expert AI"], str(t))

text, t = search(query="IT-konsult stockholm")
check("IT-konsult stockholm: Stockholm's, not Malmö's", t == ["IT-konsulter för drift"], str(t))
check("…and says where it looked", "Stockholm" in text.splitlines()[0], text.splitlines()[0])
check("a place alone is still searched as a word", split_place("stockholm") == ("stockholm", None))
check("Göteborg is a place too", split_place("städ göteborg")[1] == ("Västra Götaland", "Göteborg"))
check("'Stockholms län' drops both words", split_place("städ Stockholms län")[0] == "städ")

text, t = search(query="expert ai")
check("the same procurement from two sources is listed once", t.count("Expert AI") == 1, str(t))

text, t = search(query="ventilation")
check("open search: no award notices", t == ["Ventilation skola"], str(t))
text, t = search(query="ventilation", open_only=False)
check("open_only=false: the awards too", "Ventilation sporthall" in t, str(t))

c = TestClient(APP, base_url="https://testserver")
page = c.get("/browse", params={"q": "ventilation"}).text
check("/browse open: no award notices", "Ventilation skola" in page and "Ventilation sporthall" not in page)

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
