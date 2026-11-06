#!/usr/bin/env python3
"""Supplier pages: what a company has won, and never a person's number.

/leverantor/{name-orgnr} answers the question from the other side of the
buyer pages: what has this company won, from whom, in what? From the
contracts table (Upphandlingsmyndigheten's statistics). A sole trader's
organisation number is the owner's personal identity number, so only a
Swedish legal entity — third digit 2 or more, valid check digit — gets a
page or has its number shown, whatever the source does. These cases check
who gets a page, what it says, the links in and out, the redirect, the
sitemap and the MCP tool.

Run: python3 scripts/test_suppliers.py
"""
import asyncio
import json
import os
import pathlib
import re
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
from app import related, suppliers  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402
from mcp_shared import aggregate_contracts  # noqa: E402
from scraper import uhm_contracts  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def with_check_digit(first9):
    total = 0
    for i, ch in enumerate(first9):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return first9 + str((10 - total % 10) % 10)


SWECO = with_check_digit("556767984")      # 5567679849, the real one
NCC = with_check_digit("556613471")
SMALL = with_check_digit("559100000")
PERSON = with_check_digit("811218987")     # third digit 1: a birth month, a person
check("kontrollsiffran stämmer med Swecos riktiga nummer", SWECO == "5567679849")

print("Organisationsnummer")
check("ett aktiebolag", suppliers.is_company_number(SWECO) and suppliers.is_company_number(NCC))
check("ett personnummer är inget organisationsnummer här", not suppliers.is_company_number(PERSON))
check("fel kontrollsiffra", not suppliers.is_company_number(SWECO[:-1] + str((int(SWECO[-1]) + 1) % 10)))
check("utländska och trasiga nummer", not any(suppliers.is_company_number(x) for x in ("01148912", "", None, "55676798490")))

COLS = ("Anbudsområdes-ID;Namn för leverantör;Organisationsnummer för leverantör;Företagsstorlek för leverantör;"
        "År;Direktivstyrd;Typ av avtal;Överprövad;Huvudsaklig CPV-kod;Publiceringsdatum;Upphandlingens titel;"
        "Upphandlings-ID;Namn för köpare")
rows = []


def add(lot, name, orgnr, buyer, title, pid, year=2024, cpv="71000000 Arkitekttjänster", kind="Ramavtal",
        directive="Direktivstyrd ", appealed="Inte överprövad", size="Stora företag", day="03-01"):
    rows.append(";".join([lot, name, orgnr, size, str(year), directive, kind, appealed, cpv,
                          f"{year}-{day}", title, pid, buyer]))


# Sweco: seven contracts in six procurements from three buyers.
for i, (buyer, year) in enumerate([("GÖTEBORGS KOMMUN", 2024), ("GÖTEBORGS KOMMUN", 2023), ("GÖTEBORGS KOMMUN", 2022),
                                   ("TRAFIKVERKET", 2024), ("TRAFIKVERKET", 2021), ("UMEÅ KOMMUN", 2023)]):
    add(f"P{i}_LOT-1", "SWECO SVERIGE AB", SWECO, buyer, f"Konsulter {i}", f"P{i}", year=year,
        directive="Inte direktivstyrd " if i == 0 else "Direktivstyrd ", appealed="Överprövad" if i == 1 else "Inte överprövad")
add("P0_LOT-2", "SWECO SVERIGE AB", SWECO, "GÖTEBORGS KOMMUN", "Konsulter 0", "P0", cpv="72000000 IT-tjänster")
# NCC: five contracts, all from Trafikverket.
for i in range(5):
    add(f"N{i}_LOT-1", "NCC SVERIGE AB", NCC, "TRAFIKVERKET", f"Väg {i}", f"N{i}", cpv="45000000 Anläggningsarbete",
        kind="Kontrakt")
# A small firm with four: no page.
for i in range(4):
    add(f"S{i}_LOT-1", "LITEN KONSULT AB", SMALL, "UMEÅ KOMMUN", f"Uppdrag {i}", f"S{i}", size="Mikroföretag")
# A sole trader whose number was NOT anonymised: five contracts, still no page and no number.
for i in range(5):
    add(f"E{i}_LOT-1", "ANNA ANDERSSON", PERSON, "UMEÅ KOMMUN", f"Städ {i}", f"E{i}", size="Mikroföretag")

data = ("﻿" + COLS + "\n" + "\n".join(rows) + "\n").encode()
client = httpx.Client(transport=httpx.MockTransport(
    lambda r: httpx.Response(200, content=data, headers={"last-modified": "x", "content-length": str(len(data))})))
DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
uhm_contracts.run(DB, client=client)
conn = connect(DB)
# Göteborgs Stad has a buyer page here (five notices); Trafikverket and Umeå do not.
deadline = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
for i in range(5):
    upsert_tender(conn, {"source_system": "mercell", "source_id": f"g{i}", "tender_url": "https://example.invalid",
                         "title": f"Upphandling {i}", "authority": "Göteborgs Stad", "cpv_codes": ["71000000"],
                         "deadline": deadline, "published_at": "2026-10-01", "description": "", "value": None,
                         "procedure": None, "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})
conn.commit()
related.reset()
suppliers.reset()

print("\nVem som får en sida")
check("minst fem kontrakt och ett organisationsnummer",
      {e["orgnr"] for e in suppliers.index(conn)} == {SWECO, NCC}, str([e["name"] for e in suppliers.index(conn)]))
check("adressen: namn och nummer", suppliers.page_slug(conn, SWECO) == f"sweco-sverige-ab-{SWECO}")
check("ingen sida för den med fyra, eller för personnumret",
      suppliers.page_slug(conn, SMALL) is None and suppliers.page_slug(conn, PERSON) is None)
national = aggregate_contracts(conn, "Umeå kommun")
check("den enskilda firman räknas men varken namn eller nummer sparas eller visas",
      national["contracts"] == 10 and all(w["name"] != "Anna Andersson" and w["orgnr"] != PERSON
                                          for w in national["winners"])
      and not conn.execute("SELECT COUNT(*) FROM contracts WHERE supplier_name LIKE '%ANDERSSON%' "
                           "OR supplier_orgnr = ?", (PERSON,)).fetchone()[0], str(national))
check("personnummer och samordningsnummer känns igen, organisationsnummer inte",
      suppliers.could_be_person(PERSON) and suppliers.could_be_person(with_check_digit("811278987"))
      and not suppliers.could_be_person(SWECO) and not suppliers.could_be_person("01148912"))

print("\nLeverantörssidan")
p = suppliers.supplier_page(conn, SWECO)
check("siffrorna", p["contracts"] == 7 and p["procurements"] == 6 and p["buyer_count"] == 3 and p["years"] == "2021–2024"
      and p["size"] == "Stora företag", str({k: p[k] for k in ("contracts", "procurements", "buyer_count", "years")}))
check("köparna, flest först, med köparsida där en finns",
      p["buyers"][0] == {"name": "Göteborgs Stad", "slug": "goteborgs-stad", "n": 4}
      and {"name": "Trafikverket", "slug": None, "n": 2} in p["buyers"], str(p["buyers"]))
check("senaste tilldelningarna, en per upphandling, nyast först",
      [r["title"] for r in p["recent"]][:3] == ["Konsulter 0", "Konsulter 3", "Konsulter 1"] and len(p["recent"]) == 6,
      str([r["title"] for r in p["recent"]]))
check("andelar", p["frameworks_pct"] == 100 and p["below_threshold_pct"] == 14 and p["appealed_pct"] == 14, str(p))
text = suppliers.supplier_answer(p)
check("svaret överst", text.startswith("Sweco Sverige AB tilldelades 7 kontrakt i 6 offentliga upphandlingar från 3 köpare 2021–2024")
      and "Flest från Göteborgs Stad (4)" in text, text)

c = TestClient(APP, base_url="https://testserver")
h = c.get(f"/leverantor/sweco-sverige-ab-{SWECO}").text
check("sidan renderas med nummer, källa och länk till köparen",
      "Org.nr 556767-9849" in h and "Uppgifter: Upphandlingsmyndigheten | Bearbetning: Agentanbud" in h
      and 'href="/kopare/goteborgs-stad"' in h)
blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S)]
org = [b for b in blocks if b["@type"] == "Organization"]
check("JSON-LD: organisation med organisationsnummer, brödsmulor och FAQ",
      org and org[0]["taxID"] == SWECO and sorted(b["@type"] for b in blocks) == ["BreadcrumbList", "FAQPage", "Organization"])
r = c.get(f"/leverantor/gammalt-namn-{SWECO}", follow_redirects=False)
check("ett annat namn före numret leder till rätt adress", r.status_code == 301
      and r.headers["location"] == f"/leverantor/sweco-sverige-ab-{SWECO}")
check("okänd, för liten och personnummer: 404", all(c.get(f"/leverantor/x-{n}").status_code == 404
                                                     for n in (SMALL, PERSON, "1234567890")) and c.get("/leverantor/sweco").status_code == 404)

print("\nIndex, länkar och sitemap")
idx = c.get("/leverantor").text
check("indexet: flest kontrakt först", re.findall(r'<a href="/leverantor/([a-z0-9-]+)">', idx)
      == [f"sweco-sverige-ab-{SWECO}", f"ncc-sverige-ab-{NCC}"])
check("köparsidan länkar vinnarna", f'href="/leverantor/sweco-sverige-ab-{SWECO}"' in c.get("/kopare/goteborgs-stad").text)
sm = c.get("/sitemap.xml").text
check("sitemap", f"/leverantor/sweco-sverige-ab-{SWECO}<" in sm and "/leverantor<" in sm and PERSON not in sm)
check("sidfoten", 'href="/leverantor"' in c.get("/").text)

print("\nMCP: get_supplier")
mcp_server.DB_PATH = DB


def tool(name):
    return asyncio.run(mcp_server._get_supplier(conn, {"name": name}))[0].text


t = tool("sweco")
check("ett namn: leverantörens sammanfattning", "**Sweco Sverige AB** (org.nr 5567679849, stora företag)" in t
      and "Göteborgs Stad — 4 kontrakt" in t and f"/leverantor/sweco-sverige-ab-{SWECO}" in t, t)
check("organisationsnummer med bindestreck", "**NCC Sverige AB**" in tool(f"{NCC[:6]}-{NCC[6:]}"))
check("flera träffar: en lista att välja ur", "Flera leverantörer matchar 'sverige ab'" in tool("sverige ab"))
check("ingen träff, och personnumret hittas inte",
      "Ingen leverantör" in tool("finns inte") and "Ingen leverantör" in tool(PERSON))

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
