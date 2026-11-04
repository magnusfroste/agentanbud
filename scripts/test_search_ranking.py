#!/usr/bin/env python3
"""Search ranks what is bought above who buys it, and backs off no further
than it says.

On 4 November 2026 a visitor searched for climate and weather data —
klimatdata, väderdata, meteorolog, hydrologi — and got SMHI's fruit baskets
and HR system first (the words are in the institute's name), and for
"väderdata" bus-shelter glazing: the compound backoff, trimming two letters
at a time, went from "väderdata" past its six-letter floor to "väder". These
cases rebuild that situation on a small database.

Run: python3 scripts/test_search_ranking.py
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
from app.db import connect, upsert_tender  # noqa: E402
from app.search import MIN_PREFIX_LEN, build_match  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
conn = connect(DB)
deadline = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
SMHI = "Sveriges Meteorologiska och Hydrologiska Institut"
for i, (title, authority, desc) in enumerate([
        ("Personalsystem", SMHI, "Ett system för lön och personaladministration."),
        ("Fruktkorgar", SMHI, "Leverans av frukt till kontoren."),
        ("Mötesteknik", SMHI, "Skärmar och högtalare."),
        ("Konsulter hydrologi och hydrogeologi", "Region Gotland", "Utredningar av grundvatten."),
        ("Mätstationer", "Länsstyrelsen", "Meteorologiska mätningar av vind och nederbörd."),
        ("Glas till väderskydd", "Region Kalmar län", "Glas till busshållplatser."),
        ("Väderprognos för järnväg", "Trafikverket", "Prognoser för halka."),
]):
    upsert_tender(conn, {"source_system": "mercell", "source_id": str(i), "tender_url": "https://example.invalid",
                         "title": title, "authority": authority, "cpv_codes": [], "deadline": deadline,
                         "published_at": "2026-10-01", "description": desc, "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})
conn.commit()
c = TestClient(APP, base_url="https://testserver")


def titles(q):
    j = c.get("/api/tenders", params={"q": q, "limit": 10}).json()
    return [t["title"] for t in (j.get("items") or j.get("results") or j.get("tenders") or [])]


print("Vad som köps före vem som köper")
t = titles("hydrologi")
check("hydrologi: titelträffen först", t and t[0] == "Konsulter hydrologi och hydrogeologi", str(t))
t = titles("meteorolog")
check("meteorolog: beskrivningsträffen före SMHI:s fruktkorgar", t and t[0] == "Mätstationer", str(t))
check("…men SMHI:s upphandlingar finns kvar i listan", "Fruktkorgar" in t, str(t))
t = titles("Sveriges Meteorologiska")
check("söker man på köparen får man köparens", set(t) >= {"Personalsystem", "Fruktkorgar", "Mötesteknik"}, str(t))

print("\nSammansatta ord kortas inte under golvet")
check("väderdata: inga väderskydd", "Glas till väderskydd" not in titles("väderdata"), str(titles("väderdata")))
expr = build_match(conn, "väderdatainsamling")
check(f"avkortningen stannar på minst {MIN_PREFIX_LEN} tecken", expr is None
      or len(expr.split('"')[1]) >= MIN_PREFIX_LEN, str(expr))
check("ett sammansatt ord som finns kortas fortfarande ned till det",
      build_match(conn, "väderprognoserna") == '"väderprognos"*', str(build_match(conn, "väderprognoserna")))

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
