#!/usr/bin/env python3
"""Expected tenders from the contract end dates in TED award notices.

eForms award notices state how long a contract runs — an end date, or a
start or signing date and a duration — and how many renewals of what
length. Where they do, /upphandlingar/vantas uses that instead of the
four-year estimate from Upphandlingsmyndigheten's statistics, and leaves
out the statistics' framework the notice covers. scraper/ted_awards also
walks back a window a day until TED's eForms history begins (autumn 2023).
These cases check the date arithmetic, the backfill windows, the merge and
the text.

Run: python3 scripts/test_contract_end.py
"""
import asyncio
import json
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
from scraper import ted_awards, uhm_contracts  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


ce = renewals.contract_end
print("Datum ur eForms")
check("slutdatum och förlängningar med längd",
      ce({"contract-duration-end-date-lot": ["2029-08-31Z"], "renewal-maximum-lot": ["3"],
          "renewal-description-lot": {"swe": ["Förlängning är 12 månader"]}}) == (date(2029, 8, 31), date(2032, 8, 31), 3))
check("signeringsdatum plus längd i år",
      ce({"duration-period-value-lot": ["2"], "duration-period-unit-lot": ["YEAR"],
          "contract-conclusion-date": ["2026-07-07Z"]}) == (date(2028, 7, 7), date(2028, 7, 7), 0))
check("startdatum går före signeringsdatum, längd i månader",
      ce({"duration-period-value-lot": ["18"], "duration-period-unit-lot": ["MONTH"],
          "contract-duration-start-date-lot": ["2026-01-31Z"], "contract-conclusion-date": ["2025-12-01Z"]})[0]
      == date(2027, 7, 31))
check("förlängningar utan angiven längd: inget sista datum",
      ce({"contract-duration-end-date-lot": ["2027-03-31Z"], "renewal-maximum-lot": ["2"]}) == (date(2027, 3, 31), None, 2))
check("flera delar: det senaste slutdatumet",
      ce({"contract-duration-end-date-lot": ["2027-01-01Z", "2028-06-30Z"]})[0] == date(2028, 6, 30))
check("ingenting angivet", ce({"contract-conclusion-date": ["2026-07-07Z"]}) == (None, None, None))
check("fälten hämtas", {"contract-duration-end-date-lot", "renewal-maximum-lot", "renewal-description-lot",
                        "duration-period-value-lot", "contract-conclusion-date"} <= set(ted_awards.TED_AWARD_FIELDS))

print("\nHistorik bakåt, ett fönster per körning")
DB0 = tempfile.mktemp(suffix=".db")
m.create_app(DB0)
calls = []
real_walk = ted_awards.walk_notices
ted_awards.walk_notices = lambda **kw: (calls.append((kw.get("since"), kw.get("until"))) or iter(()))
c0 = connect(DB0)
first = ted_awards._backfill(c0, 90)
w1 = calls[-1]
ted_awards._backfill(c0, 90)
w2 = calls[-1]
check("första fönstret slutar dagen före lookbacken, nästa före det",
      w2[1] == (datetime.strptime(w1[0], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
      and (datetime.strptime(w1[1], "%Y%m%d") - datetime.strptime(w1[0], "%Y%m%d")).days == 89, f"{w1} {w2}")
c0.execute("UPDATE source_state SET value = '20231020' WHERE source = 'ted_awards_backfill'")
ted_awards._backfill(c0, 90)
check("stannar vid eForms början", calls[-1] == ("20230901", "20231019"), str(calls[-1]))
n = len(calls)
check("och gör sedan ingenting", ted_awards._backfill(c0, 90) == "historik klar" and len(calls) == n)
ted_awards.walk_notices = real_walk

print("\nI listan över väntade upphandlingar")
today = datetime.now(timezone.utc).date()


def iso(d):
    return d.isoformat() + "Z"


COLS = ("Anbudsområdes-ID;Namn för leverantör;Organisationsnummer för leverantör;Företagsstorlek för leverantör;"
        "År;Typ av avtal;Huvudsaklig CPV-kod;Publiceringsdatum;Upphandlingens titel;Upphandlings-ID;Namn för köpare")
four_years_ago = today.replace(year=today.year - 4) + timedelta(days=40)
rows = [";".join(["U1_LOT-1", "SWECO SVERIGE AB", "5567679849", "Stora företag", str(four_years_ago.year), "Ramavtal",
                  "72000000 IT", four_years_ago.isoformat(), "Webbutveckling konsulter", "U1", "GÖTEBORGS KOMMUN"]),
        ";".join(["U2_LOT-1", "NCC SVERIGE AB", "5566134929", "Stora företag", str(four_years_ago.year), "Ramavtal",
                  "45000000 Bygg", four_years_ago.isoformat(), "Markentreprenad", "U2", "GÖTEBORGS KOMMUN"])]
data = ("﻿" + COLS + "\n" + "\n".join(rows) + "\n").encode()
DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
uhm_contracts.run(DB, client=httpx.Client(transport=httpx.MockTransport(
    lambda r: httpx.Response(200, content=data, headers={"last-modified": "x"}))))
conn = connect(DB)


def award(no, title, fields, published, cpv="30200000", authority="Göteborgs Stad"):
    rec = {"publication-number": no, **fields}
    upsert_tender(conn, {"source_system": "ted_awards", "source_id": no, "tender_url": "https://example.invalid",
                         "title": title, "authority": authority, "cpv_codes": [cpv], "deadline": None,
                         "published_at": published, "description": "", "value": None, "procedure": None,
                         "contract_type": None, "document_type": "Contract Award Notice", "region": "Sverige",
                         "winner_name": ["Atea Sverige AB", "Dustin Sverige AB"], "raw_json": json.dumps(rec)})


award("1-2024", "Datorer och skärmar", {"contract-duration-end-date-lot": [iso(today + timedelta(days=30))],
                                         "renewal-maximum-lot": ["1"],
                                         "renewal-description-lot": {"swe": ["Förlängning är 6 månader"]}},
      (today - timedelta(days=700)).isoformat())
award("1-2025", "Datorer och skärmar", {"contract-duration-end-date-lot": [iso(today + timedelta(days=30))],
                                         "renewal-maximum-lot": ["1"],
                                         "renewal-description-lot": {"swe": ["Förlängning är 6 månader"]}},
      (today - timedelta(days=690)).isoformat())      # the same notice published again
award("2-2024", "Skrivare", {"contract-duration-end-date-lot": [iso(today + timedelta(days=90))],
                             "renewal-maximum-lot": ["2"]}, (today - timedelta(days=600)).isoformat())
award("5-2024", "Nybyggnation av skola", {"contract-duration-end-date-lot": [iso(today + timedelta(days=60))],
                                           "framework-agreement-lot": ["none"], "contract-nature-main-proc": "works"},
      (today - timedelta(days=500)).isoformat(), cpv="45210000")   # a building finished is not bought again
award("6-2024", "Ramavtal underhåll av vägar", {"contract-duration-end-date-lot": [iso(today + timedelta(days=150))],
                                                "framework-agreement-lot": ["fa-wo-rc"], "contract-nature-main-proc": "works"},
      (today - timedelta(days=500)).isoformat(), cpv="45233000")   # a works framework is
award("3-2024", "Telefoni", {"contract-duration-end-date-lot": [iso(today + timedelta(days=900))]},
      (today - timedelta(days=300)).isoformat(), cpv="64200000")
# The award notice for the statistics' IT framework U1, a few months after it.
award("4-2023", "Ramavtal webbutveckling", {"contract-duration-end-date-lot": [iso(today + timedelta(days=200))]},
      (four_years_ago + timedelta(days=120)).isoformat(), cpv="72000000")
conn.commit()
related.reset()
renewals.reset()
suppliers.reset()

items = renewals.expected(conn, within_months=12, limit=50)
titles = [x["title"] for x in items]
check("efter när avtalet senast löper ut; dubbletten en gång; det för avlägsna borta",
      titles == ["Markentreprenad", "Skrivare", "Ramavtal underhåll av vägar", "Ramavtal webbutveckling",
                 "Datorer och skärmar"], str(titles))
check("en byggentreprenad som tar slut väntas inte igen, ett ramavtal för bygg gör det",
      "Nybyggnation av skola" not in titles and "Ramavtal underhåll av vägar" in titles)
check("de nya fälten hämtas", {"framework-agreement-lot", "contract-nature-main-proc"} <= set(ted_awards.TED_AWARD_FIELDS))
check("statistikens ramavtal som TED-annonsen täcker visas inte två gånger", "Webbutveckling konsulter" not in titles)
pc = next(x for x in items if x["title"] == "Datorer och skärmar")
check("avtalet löper till, med förlängningar till", pc["exact"] and pc["end_text"] and pc["final_text"]
      and pc["latest"] == renewals._add_months(today + timedelta(days=30), 6).isoformat(), str(pc))
pr = next(x for x in items if x["title"] == "Skrivare")
check("förlängningar utan längd: slutdatumet, och hur många", pr["final_text"] is None and pr["renewals"] == 2
      and pr["latest"] == (today + timedelta(days=90)).isoformat())
check("vem som har avtalet enligt TED", [s["name"] for s in pc["incumbents"]] == ["Atea Sverige AB", "Dustin Sverige AB"])
mk = next(x for x in items if x["title"] == "Markentreprenad")
check("utan TED-annons: uppskattningen som förut", not mk["exact"])

c = TestClient(APP, base_url="https://testserver")
h = c.get("/upphandlingar/vantas").text
check("sidan: löper till, enligt TED-annonsen, och uppskattning för resten",
      "avtalet löper till" in h and "enligt TED-annonsen" in h and "kan förlängas 2 gånger" in h and "uppskattning)" in h)
mcp_server.DB_PATH = DB
t = asyncio.run(mcp_server._expected_tenders(conn, {}))[0].text
check("MCP-verktyget säger samma sak", "Avtalet löper till" in t and "med förlängningar till" in t
      and "(uppskattning;" in t and "TED (EU) och Upphandlingsmyndigheten" in t, t[:500])

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
