#!/usr/bin/env python3
"""Contracts awarded in Sweden, from Upphandlingsmyndigheten's open statistics.

Until October 2026 the only winners here came from TED: above the EU
thresholds, and nothing from e-Avrop, whose robots.txt forbids reading it.
The agency publishes every registered database's contracts once a year as
one CSV file. These cases import a small file shaped like the real one
(served by a mock, no network) and check what is kept, what is anonymised,
that an unchanged file is not read again, that a cut-off file cannot empty
the table, and where the figures appear: get_winner_history, /api/winners,
the buyer page and the tender page.

Run: python3 scripts/test_uhm_contracts.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app import related  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402
from mcp_shared import aggregate_contracts, pretty_company  # noqa: E402
from scraper import uhm_contracts  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


COLS = ("Anbudsområdes-ID;Kontrakterad;Namn för leverantör;Organisationsnummer för leverantör;"
        "Företagsstorlek för leverantör;År;Annonsdatabas;Direktivstyrd;Förfarande;Typ av avtal;"
        "Överprövad;Huvudsaklig CPV-kod;Publiceringsdatum;Upphandlingens titel;Upphandlings-ID;"
        "Namn för köpare;Organisationsnummer för köpare;Sektor för köpare")


def line(lot, name, orgnr, size, year, db, directive, appealed, cpv, title, pid, buyer):
    return ";".join([lot, "Kontrakterad", name, orgnr, size, year, db, directive, "Öppet förfarande",
                     "Ramavtal", appealed, cpv, f"{year}-03-01", title, pid, buyer, "2120001234", "Kommun"])


ROWS = [
    line("A1_LOT-1", "SWECO SVERIGE AB", "5560000001", "Stora företag", "2023", "e-Avrop", "Inte direktivstyrd ",
         "Inte överprövad", "71000000 Arkitekttjänster", "Konsulter bygg", "A1", "GÖTEBORGS KOMMUN"),
    line("A1_LOT-1", "WSP SVERIGE AB", "5560000002", "Stora företag", "2023", "e-Avrop", "Inte direktivstyrd ",
         "Inte överprövad", "71000000 Arkitekttjänster", "Konsulter bygg", "A1", "GÖTEBORGS KOMMUN"),
    line("A2_LOT-1", "SWECO SVERIGE AB", "5560000001", "Stora företag", "2024", "Mercell Annonsdatabas",
         "Direktivstyrd ", "Överprövad", "71300000 Tekniska tjänster", "Projektering", "A2", "GÖTEBORGS KOMMUN"),
    line("A3_LOT-1", "PERSONUPPGIFT", "Personuppgift", "Mikroföretag", "2022", "KommersAnnons.se",
         "Inte direktivstyrd ", "Inte överprövad", "77310000 Plantering", "Gräsklippning", "A3", "GÖTEBORGS KOMMUN"),
    line("A3_LOT-1", "PERSONUPPGIFT", "Personuppgift", "Mikroföretag", "2022", "KommersAnnons.se",
         "Inte direktivstyrd ", "Inte överprövad", "77310000 Plantering", "Gräsklippning", "A3", "GÖTEBORGS KOMMUN"),
    line("A4_LOT-2", "BYGG OCH MARK I NORR AB", "5560000003", "Små företag", "2021", "e-Avrop",
         "Uppgift saknas", "Uppgift saknas", "45000000 Anläggningsarbete", "Markarbeten", "A4", "GÖTEBORGS KOMMUN"),
    line("B1_LOT-1", "NCC SVERIGE AB", "5560000004", "Stora företag", "2024", "Mercell Annonsdatabas",
         "Direktivstyrd ", "Inte överprövad", "45000000 Anläggningsarbete", "Ny väg", "B1", "TRAFIKVERKET"),
]


def body(rows):
    return ("﻿" + COLS + "\n" + "\n".join(rows) + "\n").encode()


served = {"body": body(ROWS), "modified": "Thu, 24 Sep 2026 06:40:07 GMT", "gets": 0}


def handler(request):
    served["gets"] += 1
    return httpx.Response(200, content=served["body"],
                          headers={"last-modified": served["modified"], "content-length": str(len(served["body"]))})


client = httpx.Client(transport=httpx.MockTransport(handler))
DB = tempfile.mktemp(suffix=".db")

print("Import")
check("alla rader importeras", uhm_contracts.run(DB, client=client) == 7)
conn = connect(DB)
rows = {(r["lot_id"], r["supplier_key"]): dict(r) for r in conn.execute("SELECT * FROM contracts")}
s = rows[("A2_LOT-1", "5560000001")]
check("fälten", s["supplier_name"] == "SWECO SVERIGE AB" and s["cpv"] == "71300000" and s["year"] == 2024
      and s["above_threshold"] == 1 and s["appealed"] == 1 and s["buyer_key"] == "göteborgs kommun"
      and s["kind"] == "Ramavtal" and s["ad_database"] == "Mercell Annonsdatabas", str(s))
anon = [r for k, r in rows.items() if k[0] == "A3_LOT-1"]
check("anonymiserade enskilda firmor: två rader, utan namn och nummer",
      len(anon) == 2 and all(r["supplier_name"] is None and r["supplier_orgnr"] is None for r in anon), str(anon))
check("'Uppgift saknas' blir okänt", rows[("A4_LOT-2", "5560000003")]["above_threshold"] is None
      and rows[("A4_LOT-2", "5560000003")]["appealed"] is None)

print("\nOförändrad fil, ny fil, avklippt fil")
check("samma fil igen: läses inte om", uhm_contracts.run(DB, client=client) == 0
      and conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 7)
served["modified"] = "Thu, 24 Sep 2027 06:40:07 GMT"
served["body"] = body(ROWS[:6] + [ROWS[6].replace("NCC SVERIGE AB", "PEAB SVERIGE AB").replace("5560000004", "5560000005")])
check("ny fil: rader som försvunnit tas bort", uhm_contracts.run(DB, client=client) == 7
      and {r[0] for r in conn.execute("SELECT supplier_name FROM contracts WHERE lot_id = 'B1_LOT-1'")} == {"PEAB SVERIGE AB"})
served["modified"] = "Fri, 25 Sep 2027 06:40:07 GMT"
served["body"] = body(ROWS[:2])
uhm_contracts.run(DB, client=client)
check("avklippt fil (färre än hälften): gamla rader behålls",
      conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 7)
check("…och felet loggas", conn.execute("SELECT status FROM sync_log WHERE source = 'uhm_contracts' "
                                         "ORDER BY id DESC LIMIT 1").fetchone()[0] == "error")

print("\nVem vinner")
a = aggregate_contracts(conn, "Göteborgs Stad")
check("'Göteborgs Stad' hittar 'GÖTEBORGS KOMMUN'", a["contracts"] == 6 and a["procurements"] == 4, str(a))
check("vinnare med vanliga versaler, anonyma namnges inte",
      [(w["name"], w["n"]) for w in a["winners"]] == [("Sweco Sverige AB", 2), ("Bygg och Mark i Norr AB", 1),
                                                      ("WSP Sverige AB", 1)], str(a["winners"]))
check("andelar: små/medelstora, under tröskel, överprövade",
      a["sme_pct"] == 50 and a["below_threshold_pct"] == 67 and a["appealed_pct"] == 33, str(a))
check("CPV-filter", aggregate_contracts(conn, "", "45")["contracts"] == 2)
check("namn i versaler", pretty_company("SVEVIA AB (PUBL)") == "Svevia AB (publ)"
      and pretty_company("NCC SVERIGE AB") == "NCC Sverige AB" and pretty_company("Redan Rätt AB") == "Redan Rätt AB")

mcp_server.DB_PATH = DB
text = asyncio.run(mcp_server._get_winner_history(conn, {"authority": "Göteborgs Stad"}))[0].text
check("MCP: svarar även utan TED-tilldelningar, med källa",
      "Tilldelade kontrakt i Sverige 2021–2024" in text and "Sweco Sverige AB** (org.nr 5560000001) — 2 kontrakt" in text
      and "Uppgifter: Upphandlingsmyndigheten | Bearbetning: Agentanbud" in text, text)

c = TestClient(m.create_app(DB), base_url="https://testserver")
j = c.get("/api/winners?authority=Trafikverket").json()
check("REST: under 'sweden', skilt från TED", j["sweden"]["contracts"] == 1 and j["sweden"]["winners"][0]["name"] == "Peab Sverige AB"
      and j["contracts"] == 0 and "Upphandlingsmyndigheten" in j["sweden"]["source"], json.dumps(j)[:300])
check("REST: inget 'sweden' där inget finns", c.get("/api/winners?authority=Finns+inte").json()["sweden"] is None)

print("\nKöparsidan och upphandlingen")
for i in range(5):
    upsert_tender(conn, {
        "source_system": "mercell", "source_id": f"g{i}", "tender_url": "https://example.invalid",
        "title": f"Upphandling {i}", "authority": "Göteborgs Stad", "cpv_codes": ["71000000"],
        "deadline": (datetime.now(timezone.utc) + timedelta(days=10 + i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_at": "2026-10-01", "description": "", "value": None, "procedure": None,
        "contract_type": None, "document_type": None, "region": "Västra Götalands län", "raw_json": "{}"})
conn.commit()
related.reset()
page = related.buyer_page(conn, "goteborgs-stad")
check("köparen 'Göteborgs Stad' får kontrakten från 'GÖTEBORGS KOMMUN'",
      page and page["national"]["contracts"] == 6 and page["national"]["years"] == "2021–2024"
      and page["national"]["winners"][0] == {"name": "Sweco Sverige AB", "n": 2}, str(page and page["national"]))
answer = related.buyer_answer(page)
check("svaret överst nämner vinnarna och källan", "2021–2024 gick flest kontrakt till Sweco Sverige AB (2)" in answer
      and "Upphandlingsmyndighetens statistik" in answer, answer)
h = c.get("/kopare/goteborgs-stad").text
check("sidan visar listan med källa", "Vem vinner hos Göteborgs Stad, 2021–2024" in h
      and "Uppgifter: Upphandlingsmyndigheten | Bearbetning: Agentanbud" in h)
tid = conn.execute("SELECT id FROM tenders WHERE source_id = 'g0'").fetchone()[0]
check("upphandlingssidan nämner dem under Om köparen",
      "Flest kontrakt 2021–2024" in c.get(f"/tenders/{tid}").text)
check("matchningsnyckeln", related.buyer_match_key("AB Borlänge Energi") == related.buyer_match_key("BORLÄNGE ENERGI AB")
      and related.buyer_match_key("Malmö stad") == "malmö kommun")

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
