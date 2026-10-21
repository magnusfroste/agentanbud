#!/usr/bin/env python3
"""Industry and county pages: open tenders only, the right ones, said plainly.

/upphandlingar/bransch/{slug} and /upphandlingar/lan/{slug} are the pages a
search engine can index where /browse?… is not. These cases check what each
page counts and lists, the answer text on top, what it leaves out (closed,
award notices, and TED on county pages), the links into them and the sitemap.

Run: python3 scripts/test_hubs.py
"""
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import hubs  # noqa: E402
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
n = 0


def add(title, cpv, deadline, authority="Örebro kommun", region="Örebro län", source="mercell"):
    global n
    n += 1
    upsert_tender(conn, {
        "source_system": source, "source_id": str(n), "tender_url": "https://example.invalid", "title": title,
        "authority": authority, "cpv_codes": cpv, "deadline": deadline, "published_at": "2026-10-01",
        "description": "", "value": None, "procedure": None, "contract_type": None, "document_type": None,
        "region": region, "raw_json": "{}"})


add("Ventilation skola", ["45331000"], iso(5))
add("Stambyte", ["45332000"], iso(40), authority="Örebrobostäder AB, ÖREBRO")
add("Ventilation sjukhus", ["45331000"], iso(60), authority="Region Stockholm", region="Stockholms län")
add("Gammal ventilation", ["45331000"], iso(-3))                                   # closed
add("Tilldelad ventilation", ["45331000"], None, source="ted_awards", region="Sverige")
add("EU-ventilation", ["45331000"], iso(30), authority="Fortifikationsverket, ESKILSTUNA",
    region="Sverige", source="ted")
add("Nybyggnad förskola", ["45210000"], iso(20))                                    # bygg, not VVS
add("Ny webbplats", ["72000000"], iso(15), region="Sverige, Östra Sverige, Stockholm, Stockholms län",
    source="kommersannons", authority="Täby kommun")
conn.commit()

print("Vad en branschsida räknar")
vvs = hubs.find("bransch", "vvs")
page = hubs.hub_page(conn, vvs)
titles = [t["title"] for t in page["items"]]
check("öppna VVS-upphandlingar, alla källor", page["total"] == 4, f"{titles}")
check("stängda och tilldelningar är inte med", "Gammal ventilation" not in titles and "Tilldelad ventilation" not in titles)
check("bygg utan VVS-kod är inte med", "Nybyggnad förskola" not in titles)
check("sorterade efter sista anbudsdag", titles[0] == "Ventilation skola")
check("stänger inom två veckor", page["closing_soon"] == 1)
check("bygg innehåller VVS (45 är förälder till 4533)", hubs.hub_page(conn, hubs.find("bransch", "bygg"))["total"] == 5)

print("\nSvaret överst")
text = hubs.answer(page, "2026-10-20T06:02:00Z")
check("antal och fras", text.startswith("Just nu finns 4 öppna upphandlingar inom VVS, värme och ventilation"), text)
check("versaler behålls: VVS, IT", vvs.phrase == "inom VVS, värme och ventilation"
      and hubs.find("bransch", "it").phrase == "inom IT-tjänster och programvara"
      and hubs.find("bransch", "bygg").phrase == "inom bygg och anläggning")
check("flest annonser: köpare med fler än en", "Örebro kommun" not in text or "(1)" not in text.split("Flest")[-1])
check("källorna nämns", "Källor: Mercell och TED." in text, text)
check("datum på svenska", "Uppdaterad 20 oktober 2026." in text)
empty = hubs.hub_page(conn, hubs.find("bransch", "juridik"))
check("tom sida säger det", hubs.answer(empty, None).startswith("Just nu finns inga öppna upphandlingar inom juridiska"))

print("\nVad en länssida räknar")
orebro = hubs.hub_page(conn, hubs.find("lan", "orebro"))
check("öppna med regionen Örebro län", orebro["total"] == 3, f"{[t['title'] for t in orebro['items']]}")
sthlm = hubs.hub_page(conn, hubs.find("lan", "stockholm"))
check("Kommersannons långa regionsträng matchar länet",
      "Ny webbplats" in [t["title"] for t in sthlm["items"]] and sthlm["total"] == 2)
check("TED (region 'Sverige') är inte med på något län",
      all("EU-ventilation" not in [t["title"] for t in hubs.hub_page(conn, h)["items"]] for h in hubs.COUNTIES))

print("\nSidorna")
c = TestClient(APP, base_url="https://testserver")
r = c.get("/upphandlingar/bransch/vvs")
h = r.text
check("200 och titel", r.status_code == 200
      and "Öppna upphandlingar inom VVS, värme och ventilation | Agentanbud" in h)
check("meta description är svaret", 'name="description" content="Just nu finns 4 öppna' in h)
blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S)]
check("BreadcrumbList och FAQPage", sorted(b["@type"] for b in blocks) == ["BreadcrumbList", "FAQPage"])
check("listan länkar till upphandlingarna", h.count('href="/tenders/') == 4)
check("API-tips med CPV", "/api/tenders?cpv=4533" in h)
check("länssida förklarar varför TED saknas", "TED" in c.get("/upphandlingar/lan/orebro").text)
check("okänd sida 404", c.get("/upphandlingar/bransch/finns-inte").status_code == 404)
check("okänd typ 404", c.get("/upphandlingar/kommun/orebro").status_code == 404)
idx = c.get("/upphandlingar").text
check("indexet listar alla sidor med antal",
      all(hub.path in idx for hub in hubs.SECTORS + hubs.COUNTIES) and 'vvs">VVS, värme och ventilation</a> <span class="muted">(4)' in idx)
check("gamla adresser fungerar: ?q → /browse", c.get("/upphandlingar?q=x", follow_redirects=False).headers["location"] == "/browse?q=x")
tid = conn.execute("SELECT id FROM tenders WHERE title = 'Stambyte'").fetchone()[0]
check("…och /upphandlingar/{id} → /tenders/{id}",
      c.get(f"/upphandlingar/{tid}", follow_redirects=False).headers["location"] == f"/tenders/{tid}")

print("\nLänkar in")
d = c.get(f"/tenders/{tid}").text
check("upphandlingen länkar till sin bransch och sitt län",
      'href="/upphandlingar/bransch/vvs"' in d and 'href="/upphandlingar/lan/orebro"' in d)
ted_id = conn.execute("SELECT id FROM tenders WHERE title = 'EU-ventilation'").fetchone()[0]
check("TED-upphandling länkar till branschen men inte till ett län",
      'href="/upphandlingar/bransch/vvs"' in c.get(f"/tenders/{ted_id}").text
      and "/upphandlingar/lan/" not in c.get(f"/tenders/{ted_id}").text)
check("sidfoten länkar till indexet", 'href="/upphandlingar"' in c.get("/").text)
sm = c.get("/sitemap.xml").text
check("sitemap har alla sidor", all(f"https://www.agentanbud.se{hub.path}<" in sm for hub in hubs.SECTORS + hubs.COUNTIES))

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
