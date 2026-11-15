#!/usr/bin/env python3
"""A search word that names a CPV code finds the tenders with that code,
after the ones that have the word, and says why each such tender is there.
Only specific words do. Offline.

Run: python3 scripts/test_cpv_query.py
"""
import asyncio
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402
from app.search import cpv_for_query  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


print("Which codes a search names")
check("reservkraft → Reservkraftssystem", ("3168251", "Reservkraftssystem") in cpv_for_query("reservkraft"))
check("nödström, through its Swedish word, too", cpv_for_query("nödström") == cpv_for_query("reservkraft"))
check("städning → 9091 Städning (not the whole of 909 'Städning och renhållning')",
      list(cpv_for_query("städning")) == [("9091", "Städning")], str(cpv_for_query("städning")))
check("several words: every one in the name", list(cpv_for_query("bärbara datorer")) == [("302131", "Bärbara datorer")],
      str(cpv_for_query("bärbara datorer")))
check("meteorologiska → the instruments, not navigation",
      list(cpv_for_query("meteorologiska")) == [("3812", "Meteorologiska instrument")], str(cpv_for_query("meteorologiska")))
check("a word naming too many codes names none", list(cpv_for_query("programvara")) == [])
check("a short word names none", list(cpv_for_query("väder")) == [])

DB = tempfile.mktemp(suffix=".db")
m.create_app(DB)
conn = connect(DB)
soon = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
for sid, title, desc, cpv, authority in [
        ("1", "Reservkraftverk till skola", "Nytt reservkraftverk.", ["31120000"], "Kinda kommun"),
        ("2", "Aggregat till vattenverk", "Diesel, 400 kVA.", ["31682510"], "Kinda kommun"),
        ("3", "Datorer till elever", "1 200 enheter.", ["30213100"], "Uppsala kommun"),
        ("4", "Lampor till förråd", "Ficklampor.", ["31521300"], "Uppsala kommun"),
        ("5", "Fruktkorgar", "Frukt.", ["15300000"], "Reservkraft AB"),
]:
    upsert_tender(conn, {"source_system": "mercell", "source_id": sid, "tender_url": "https://example.invalid",
                         "title": title, "authority": authority, "cpv_codes": cpv, "deadline": soon,
                         "published_at": "2026-10-01", "description": desc, "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})
conn.commit()


def search(**args):
    text = asyncio.run(mcp_server._search_tenders_uncached(conn, args))[0].text
    return text, re.findall(r"\*\*(.+?)\*\*", text)


print("\nIn search_tenders")
text, t = search(query="reservkraft")
check("the tender with the word first, the one with the code after",
      t[:2] == ["Reservkraftverk till skola", "Aggregat till vattenverk"], str(t))
check("…and the second says it is there by its code",
      "Träff via CPV: 31682510 Reservkraftssystem" in text, text)
check("…the first does not", text.count("Träff via CPV") == 1, text)
text, t = search(query="nödström")
check("nödström finds the aggregate by its code", "Aggregat till vattenverk" in t, str(t))
text, t = search(query="bärbara datorer")
check("bärbara datorer: the computers, not the lamps", t == ["Datorer till elever"], str(t))
check("a list does not print the descriptions", "1 200 enheter" not in text, text)
text, t = search(query="reservkraft", cpv="3112")
check("a CPV code of the caller's own is not widened", t == ["Reservkraftverk till skola"], str(t))

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
