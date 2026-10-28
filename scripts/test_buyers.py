#!/usr/bin/env python3
"""Buyer pages: one per buyer with enough to say, from the snapshot.

/kopare/{slug} answers "what does this buyer buy, and who wins?" — the
question a supplier asks before bidding and an answer engine is asked. These
cases check who gets a page, the name and address it gets, what it lists,
the text on top, the links in and the sitemap.

Run: python3 scripts/test_buyers.py
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


def add(title, authority, days, cpv=("45331000",), source="mercell", winner=None):
    n[0] += 1
    upsert_tender(conn, {
        "source_system": source, "source_id": str(n[0]), "tender_url": "https://example.invalid", "title": title,
        "authority": authority, "cpv_codes": list(cpv), "deadline": iso(days) if days is not None else None,
        "published_at": "2026-10-01", "description": "", "value": None, "procedure": None, "contract_type": None,
        "document_type": None, "region": "Örebro län", "raw_json": "{}"})
    if winner:
        conn.execute("UPDATE tenders SET winner_name = ? WHERE source_id = ?", (json.dumps([winner]), str(n[0])))
    return n[0]


# Örebro kommun: six notices (three open), written two ways, and two awards.
ids = [add("Ventilation skola", "Örebro kommun, ÖREBRO", 20),
       add("Snöröjning", "ÖREBRO KOMMUN", 5, cpv=("90620000",)),
       add("IT-drift", "Örebro kommun", 40, cpv=("72000000",))]
for i in range(3):
    add(f"Gammal {i}", "Örebro kommun", -30)
add("Tilldelad 1", "Örebro kommun", None, source="ted_awards", winner="Bravida Sverige AB")
add("Tilldelad 2", "ÖREBRO KOMMUN", None, source="ted_awards", winner="BRAVIDA SVERIGE AB")
# Åre kommun: five notices, all closed. Ale kommun: only two — no page.
for i in range(5):
    add(f"Åre {i}", "Åre kommun", -10)
add("Ale 1", "Ale kommun", 10)
add("Ale 2", "Ale kommun", -10)
conn.commit()
related.reset()

print("Vem som får en sida")
check("adresser utan å/ä/ö", related.slugify("Västra Götalandsregionen") == "vastra-gotalandsregionen"
      and related.slugify("Örebro kommun") == "orebro-kommun")
slugs = set(related.buyer_slugs(conn))
check("fem annonser eller mer ger en sida, färre inte", slugs == {"orebro-kommun", "are-kommun"}, str(slugs))

print("\nKöparsidan")
p = related.buyer_page(conn, "orebro-kommun")
check("namnet skrivs som oftast, inte med versaler", p["name"] == "Örebro kommun", p["name"])
check("öppna upphandlingar, båda skrivsätten, närmast deadline först",
      [t["title"] for t in p["open"]] == ["Snöröjning", "Ventilation skola", "IT-drift"], str([t["title"] for t in p["open"]]))
check("annonser i data", p["notices"] == 6)
check("vinnare med skrivsätten sammanslagna", p["winners"] == [{"name": "Bravida Sverige AB", "n": 2}], str(p["winners"]))
check("branscher länkar till branschsidan", any(u["hub"] == "/upphandlingar/bransch/bygg" for u in p["usual"]), str(p["usual"]))
text = related.buyer_answer(p)
check("svaret överst", text.startswith("Örebro kommun har 3 öppna upphandlingar på Agentanbud just nu och 6 annonser")
      and "Bravida Sverige AB (2)" in text and "Köper oftast: anläggningsarbete; " in text, text)
check("okänd sida", related.buyer_page(conn, "finns-inte") is None)

c = TestClient(APP, base_url="https://testserver")
h = c.get("/kopare/orebro-kommun").text
check("sidan renderas med titel och svar", "Upphandlingar från Örebro kommun | Agentanbud" in h and text[:40] in h)
blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S)]
check("BreadcrumbList och FAQ med vinnarfrågan", sorted(b["@type"] for b in blocks) == ["BreadcrumbList", "FAQPage"]
      and len([b for b in blocks if b["@type"] == "FAQPage"][0]["mainEntity"]) == 2)
check("öppna länkas", h.count('class="card-tender"') == 3)
check("en köpare utan öppna säger det", "Inga öppna upphandlingar just nu" in c.get("/kopare/are-kommun").text)
check("ingen sida: 404", c.get("/kopare/ale-kommun").status_code == 404)

print("\nIndex, länkar och sitemap")
idx = c.get("/kopare").text
order = re.findall(r'<a href="/kopare/([a-z-]+)">', idx)
check("svensk bokstavsordning: Å före Ö", order == ["are-kommun", "orebro-kommun"], str(order))
d = c.get(f"/tenders/{ids[0]}").text
check("upphandlingen länkar till köparsidan", 'href="/kopare/orebro-kommun"' in d and "Allt om Örebro kommun" in d)
ale = conn.execute("SELECT id FROM tenders WHERE title = 'Ale 1'").fetchone()[0]
check("men inte där sidan saknas", "/kopare/" not in c.get(f"/tenders/{ale}").text.split("Om köparen")[1].split("</div>")[0])
gone = conn.execute("SELECT id FROM tenders WHERE title = 'Åre 0'").fetchone()[0]
check("stängd hos köpare utan öppna: 'inga öppna', inte 'inga andra'",
      "inga öppna upphandlingar" in c.get(f"/tenders/{gone}").text)
sm = c.get("/sitemap.xml").text
check("sitemap har köparsidorna", "/kopare/orebro-kommun<" in sm and "/kopare/are-kommun<" in sm and "/kopare<" in sm)
check("sidfoten länkar till köparna", 'href="/kopare"' in c.get("/").text)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
