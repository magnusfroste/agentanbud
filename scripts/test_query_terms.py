#!/usr/bin/env python3
"""German and English searches find Swedish tenders.

On 4 November 2026 a visitor searched "Klimadatensatz", "wetterdaten" and
"klimadaten"; that month's searches with nothing found also held
"sustainability", "circular business model" and "foresight" — while the
database had Swedish tenders for each. app/query_terms.py maps such words to
their Swedish equivalents, and the search looks for both. These cases check
that the foreign words now find the Swedish tenders, that an English title
is still found by its own words, that Swedish searches are unchanged, and
that the words leave the "searched, nothing found" list on /analytics.

Run: python3 scripts/test_query_terms.py
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
from app import insights  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402
from app.query_terms import swedish_for  # noqa: E402
from app.search import build_match  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


print("Ordlistan")
check("tyska sammansättningar via sin början", swedish_for("Klimadatensatz") == ("klimatdata", "klimat"))
check("ß som i frågan", swedish_for("Straßenbau") == swedish_for("strassenbau") == ("väg", "anläggning"))
check("hela fraser", swedish_for("Circular  Business Model") == ("cirkulär affärsmodell", "cirkulär"))
check("hochwasser (sett i loggen 7 november)", swedish_for("Hochwasserschutz") == ("högvatten", "översvämning"))
check("svenska ord lämnas i fred", swedish_for("ventilation") == () and swedish_for("klimat") == ())

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
conn = connect(DB)
deadline = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
for i, (title, desc) in enumerate([
        ("Väderprognos för järnväg", "Prognoser för halka och snö."),
        ("Klimatberäkningsverktyg", "Verktyg för klimatberäkningar."),
        ("Ramavtal hållbarhetskonsult", "Stöd i hållbarhetsarbete."),
        ("Cirkulär affärsmodell för möbler", "Återbruk av möbler."),
        ("Omvärldsanalys och framtidsscenarier", "Strategiskt stöd."),
        ("Lokalvård skolor", "Städning av skolor."),
        ("Cleaning services for embassy", "English notice on TED."),
        ("Ventilation sporthall", "Byte av aggregat."),
]):
    upsert_tender(conn, {"source_system": "ted", "source_id": str(i), "tender_url": "https://example.invalid",
                         "title": title, "authority": "Testkommun", "cpv_codes": [], "deadline": deadline,
                         "published_at": "2026-10-01", "description": desc, "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})
conn.commit()
c = TestClient(APP, base_url="https://testserver")


def titles(q):
    j = c.get("/api/tenders", params={"q": q, "limit": 10}).json()
    return [t["title"] for t in (j.get("items") or j.get("results") or j.get("tenders") or [])]


print("\nUtländska sökningar hittar svenska upphandlingar")
for q, want in [("wetterdaten", "Väderprognos för järnväg"), ("Klimadatensatz", "Klimatberäkningsverktyg"),
                ("klimadaten", "Klimatberäkningsverktyg"), ("sustainability", "Ramavtal hållbarhetskonsult"),
                ("circular business model", "Cirkulär affärsmodell för möbler"),
                ("foresight", "Omvärldsanalys och framtidsscenarier"),
                ("Gebäudereinigung", "Lokalvård skolor"), ("weather data", "Väderprognos för järnväg")]:
    t = titles(q)
    check(f"{q} → {want}", want in t, str(t))
t = titles("cleaning")
check("en engelsk titel hittas fortfarande av sina egna ord, och svenska till",
      "Cleaning services for embassy" in t and "Lokalvård skolor" in t, str(t))
check("svenska sökningar oförändrade", titles("ventilation") == ["Ventilation sporthall"]
      and build_match(conn, "ventilation") == '"ventilation"*')

print("\nEfterfrågan utan träff")
check("de utländska orden räknas inte längre som luckor i datan",
      all(insights._term_has_data(conn, q, 8) for q in ("wetterdaten", "Klimadatensatz", "sustainability", "foresight")))

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
