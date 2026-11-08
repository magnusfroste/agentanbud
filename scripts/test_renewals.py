#!/usr/bin/env python3
"""Tenders to expect: framework agreements due to be put out again.

A framework may as a rule run at most four years, so one published four
years ago less a month is due within the month. app/renewals.py lists the
frameworks in Upphandlingsmyndigheten's statistics that are due, leaves out
those already passed or already tendered again, and marks those out right
now. These cases build each situation with dates relative to today, so the
test does not go stale, and check the list, the page, the industry and
buyer sections and the MCP tool.

Run: python3 scripts/test_renewals.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app import related, renewals, suppliers  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402
from scraper import uhm_contracts  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


today = datetime.now(timezone.utc).date()


def ago(years=0, days=0):
    """A publication date that many years and days before today."""
    d = today.replace(year=today.year - years) if not (today.month == 2 and today.day == 29) else today.replace(
        year=today.year - years, day=28)
    return (d - timedelta(days=days)).isoformat()


def soon(days):
    """Four years ago, plus `days`: due at the latest in `days` days."""
    return (date.fromisoformat(ago(4)) + timedelta(days=days)).isoformat()


COLS = ("Anbudsområdes-ID;Namn för leverantör;Organisationsnummer för leverantör;Företagsstorlek för leverantör;"
        "År;Typ av avtal;Huvudsaklig CPV-kod;Publiceringsdatum;Upphandlingens titel;Upphandlings-ID;Namn för köpare")
rows = []


def add(pid, title, published, cpv, kind="Ramavtal", buyer="GÖTEBORGS KOMMUN", suppliers_=(("SWECO SVERIGE AB", "5567679849"),)):
    for i, (name, orgnr) in enumerate(suppliers_):
        rows.append(";".join([f"{pid}_LOT-1", name, orgnr, "Stora företag", published[:4], kind,
                              f"{cpv} Något", published, title, pid, buyer]))


add("A", "IT-konsulter", soon(30), "72000000",
    suppliers_=(("SWECO SVERIGE AB", "5567679849"), ("WSP SVERIGE AB", "5560574880"), ("CGI SVERIGE AB", "5560502406"),
                ("ATEA SVERIGE AB", "5562286153")))
add("B", "Markentreprenad", soon(200), "45000000")
add("C", "Snöröjning", soon(-20), "90620000")                       # due already passed: left out
add("D", "Arkitekter", soon(60), "71000000")
add("D2", "Arkitekter igen", ago(1), "71000000")                    # renewed in the statistics: D left out
add("D3", "Laboratorieanalyser", ago(1), "72000000", kind="Kontrakt")  # same group as A, unrelated: A stays
add("E", "Städ", soon(40), "90910000", kind="Kontrakt")             # not a framework
add("F", "Personbilar", soon(150), "34100000")                      # open right now in our notices
add("G", "Livsmedel", soon(90), "15000000")                         # renewed among our notices (closed)
add("H", "IT-drift", soon(100), "72500000", buyer="TRAFIKVERKET")
add("I", "Möbler", soon(500), "39100000")                           # due beyond twelve months
data = ("﻿" + COLS + "\n" + "\n".join(rows) + "\n").encode()
client = httpx.Client(transport=httpx.MockTransport(
    lambda r: httpx.Response(200, content=data, headers={"last-modified": "x", "content-length": str(len(data))})))
DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
uhm_contracts.run(DB, client=client)
conn = connect(DB)


def notice(title, cpv, deadline_days, published):
    upsert_tender(conn, {"source_system": "mercell", "source_id": title, "tender_url": "https://example.invalid",
                         "title": title, "authority": "Göteborgs Stad", "cpv_codes": [cpv],
                         "deadline": (datetime.now(timezone.utc) + timedelta(days=deadline_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "published_at": published, "description": "", "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})


notice("Personbilar 2026", "34110000", 20, ago(0, 10))
notice("Livsmedel 2025", "15000000", -100, ago(1))
notice("Takbyte skola", "45000000", 25, ago(0, 3))     # same group as B, unrelated: B stays expected
for i in range(4):
    notice(f"Övrigt {i}", "79000000", 30, ago(0, 5))
conn.commit()
related.reset()
renewals.reset()
suppliers.reset()

print("Vilka som väntas")
items = renewals.expected(conn, within_months=12, limit=50)
ids = [x["id"] for x in items]
check("ute nu först, sedan de som ska upphandlas snarast", ids == ["F", "A", "H", "B"], str(ids))
check("passerade, redan omgjorda, kontrakt och för avlägsna är borta", not {"C", "D", "E", "G", "I"} & set(ids))
f = items[0]
check("den som är ute nu länkar annonsen", f["open_title"] == "Personbilar 2026" and f["open_id"])
a = items[1]
pub = date.fromisoformat(a["published"])
check("två till fyra år efter den förra", a["published"] == soon(30)
      and a["earliest"] == pub.replace(year=pub.year + 2).isoformat()
      and a["latest"] == pub.replace(year=pub.year + 4).isoformat(), f"{a['earliest']} {a['latest']}")
check("vem som har avtalet", [s["name"] for s in a["incumbents"]] == ["Atea Sverige AB", "CGI Sverige AB", "Sweco Sverige AB"]
      and a["suppliers"] == 4, str(a["incumbents"]))
check("räknat", renewals.count(conn, within_months=12) == 4 and renewals.count(conn, within_months=24) == 5)
b_item = next(x for x in items if x["id"] == "B")
check("en orelaterad öppen annons i samma CPV-grupp gör den varken 'ute nu' eller omupphandlad",
      b_item["open_id"] is None, str(b_item))
check("en orelaterad senare upphandling i statistiken räknas inte som omupphandling",
      "A" in ids and "H" in ids)
check("CPV-filter", [x["id"] for x in renewals.expected(conn, cpv="72", within_months=12)] == ["A", "H"])
check("köpare skriven som vi skriver den",
      [x["id"] for x in renewals.expected(conn, buyer="Göteborgs Stad", within_months=24)] == ["F", "A", "B", "I"])

print("\nSidorna")
c = TestClient(APP, base_url="https://testserver")
h = c.get("/upphandlingar/vantas").text
check("sidan: antal, lista, förbehåll och källa", "4 ramavtal ska upphandlas igen senast inom 12 månader" in h
      and "IT-konsulter" in h and "Uppskattning:" in h and "Uppgifter: Upphandlingsmyndigheten | Bearbetning: Agentanbud" in h)
check("ute nu-raden länkar annonsen", "<strong>Ute nu:</strong>" in h and f'href="/tenders/{f["open_id"]}"' in h)
check("per bransch", "IT-drift" in c.get("/upphandlingar/vantas", params={"bransch": "it"}).text
      and "Markentreprenad" not in c.get("/upphandlingar/vantas", params={"bransch": "it"}).text)
check("branschsidan har avsnittet", "Väntas upphandlas inom IT-tjänster och programvara" in c.get("/upphandlingar/bransch/it").text)
check("köparsidan har avsnittet", "Väntas upphandlas av Göteborgs Stad" in c.get("/kopare/goteborgs-stad").text)
check("/upphandlingar/{id} leder fortfarande till upphandlingen",
      c.get("/upphandlingar/12", follow_redirects=False).headers.get("location") == "/tenders/12")
check("sitemap och indexet", "/upphandlingar/vantas<" in c.get("/sitemap.xml").text
      and 'href="/upphandlingar/vantas"' in c.get("/upphandlingar").text)

print("\nMCP: expected_tenders")
mcp_server.DB_PATH = DB
t = asyncio.run(mcp_server._expected_tenders(conn, {"cpv": "72"}))[0].text
check("listan med vem som har avtalet och förbehållet", "**IT-konsulter** — Göteborgs kommun" in t
      and "Har avtalet: Atea Sverige AB, CGI Sverige AB, Sweco Sverige AB och 1 till." in t and "Uppskattning:" in t, t)
check("tomt svar förklarar", "Inga ramavtal" in asyncio.run(mcp_server._expected_tenders(conn, {"cpv": "99"}))[0].text)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
