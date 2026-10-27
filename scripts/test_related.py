#!/usr/bin/env python3
"""A tender page says more than the notice: CPV in words, similar open
tenders, and what the buyer usually buys and from whom.

Run: python3 scripts/test_related.py
"""
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
from app.insights import looks_like_bot  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


print("CPV i klartext")
check("en känd kod", related.cpv_label("45331000") == "Installation av värme, ventilation och luftkonditionering")
check("en finare kod faller tillbaka på närmaste förälder", related.cpv_label("45331999") == related.cpv_label("45331000"))
check("huvudgrupp", related.cpv_label("72000000").startswith("IT-tjänster"))
check("skräp ger inget", related.cpv_label("") is None and related.cpv_label("abc") is None)
check("alla 9 454 koder finns", len(related._labels()) == 9454, str(len(related._labels())))

print("\nLiknande och köparen")
DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
conn = connect(DB)
n = [0]


def iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(title, cpv, days, authority="Region Halland, HALMSTAD", source="mercell", winner=None):
    n[0] += 1
    upsert_tender(conn, {
        "source_system": source, "source_id": str(n[0]), "tender_url": "https://example.invalid", "title": title,
        "authority": authority, "cpv_codes": cpv, "deadline": iso(days) if days is not None else None,
        "published_at": "2026-10-01", "description": "", "value": None, "procedure": None, "contract_type": None,
        "document_type": None, "region": "Hallands län", "raw_json": "{}"})
    if winner:
        conn.execute("UPDATE tenders SET winner_name = ? WHERE source_id = ?", (f'["{winner}"]', str(n[0])))
    return n[0]


me = add("Ventilation sjukhus", ["45331000", "45330000"], 20)
add("Ventilation skola", ["45331000"], 10, authority="Falkenbergs kommun")      # shares 2 codes? one
add("Städning", ["90910000"], 15)                                               # same buyer
add("Stängd ventilation", ["45331000"], -5)                                     # closed
add("Annat län, annan bransch", ["72000000"], 30, authority="Region Skåne")      # unrelated
add("Region Hallandsfiske", ["45331000"], 12, authority="Region Hallandsfiske AB")  # name prefix, not buyer
add("Tilldelad ventilation", ["45331000"], None, authority="Region Halland", source="ted_awards", winner="Bravida Sverige AB")
add("Tilldelad ventilation 2", ["45331100"], None, authority="REGION HALLAND", source="ted_awards", winner="Caverion AB")
add("Tilldelad i Skåne", ["45331000"], None, authority="Region Skåne", source="ted_awards", winner="Annan AB")
conn.commit()
t = dict(conn.execute("SELECT * FROM tenders WHERE id = ?", (me,)).fetchone())
t["cpv_codes_list"] = ["45331000", "45330000"]
sim = [s["title"] for s in related.similar_open(conn, t)]
check("öppna med samma CPV-grupp eller köpare, inte stängda eller orelaterade",
      set(sim) == {"Ventilation skola", "Städning", "Region Hallandsfiske"}, str(sim))
check("samma köpare och fler gemensamma koder först", sim[0] == "Städning" or sim[0] == "Ventilation skola", str(sim))
b = related.buyer_profile(conn, "Region Halland, HALMSTAD")
check("köparens namn utan ort", b["name"] == "Region Halland")
check("öppna hos köparen, inte hos 'Region Hallandsfiske'", b["open"] == 2, str(b))
check("vinnare hos köparen i alla skrivsätt, inte i Skåne",
      sorted(w["name"] for w in b["winners"]) == ["Bravida Sverige AB", "Caverion AB"], str(b["winners"]))
add("Snöröjning", ["90620000"], 9, authority="ÖREBRO KOMMUN, Örebro")
conn.commit()
related.reset()   # the snapshot is rebuilt in the background in production
check("versaler med Å/Ä/Ö är samma köpare", related.buyer_profile(conn, "Örebro kommun")["open"] == 1)
check("köper oftast: med namn", b["usual"] and b["usual"][0]["label"].startswith("Anläggningsarbete"), str(b["usual"]))
check("för korta namn ger ingen profil", related.buyer_profile(conn, "AB") is None)

print("\nSidan")
c = TestClient(APP, base_url="https://testserver")
h = c.get(f"/tenders/{me}").text
check("CPV med namn", "Installation av värme, ventilation och luftkonditionering" in h)
check("Om köparen", "Om köparen" in h and "Region Halland har" in h and "Bravida Sverige AB (1)" in h
      and "enligt 2 tilldelningar" in h)
check("liknande öppna länkas", "Liknande öppna upphandlingar" in h and h.count('class="card-tender"') == 3)
check("länk till köparens öppna", "/browse?authority=Region%20Halland" in h)
bare = add("Utan kod och köpare", [], 5, authority="")
conn.commit()
related.reset()
check("en upphandling utan kod och köpare renderas ändå", c.get(f"/tenders/{bare}").status_code == 200)

print("\nÖgonblicksbilden")
import time as _t  # noqa: E402
snap = related._snapshot(conn)
check("samma ögonblicksbild igen, ingen ny byggs per sidvisning", related._snapshot(conn) is snap)
add("Ny efter bilden", ["45331000"], 8)
conn.commit()
check("ny data under en minut: den gamla bilden används", related._snapshot(conn) is snap)
snap.built -= related.MIN_REBUILD_S + 1
check("efter en minut med ny data: gamla bilden svarar medan en ny byggs", related._snapshot(conn) is snap)
for _ in range(100):
    if related._SNAPSHOTS[related._path(conn)] is not snap:
        break
    _t.sleep(0.02)
check("…och den nya tar över", related._SNAPSHOTS[related._path(conn)] is not snap
      and "Ny efter bilden" in [x["title"] for x in related.similar_open(conn, t, limit=10)])
gone = add("Stänger om ett ögonblick", ["45331000"], 0.00002)
conn.commit()
related.reset()
related._snapshot(conn)
_t.sleep(3)
check("en deadline som passerar försvinner utan ombyggnad",
      "Stänger om ett ögonblick" not in [x["title"] for x in related.similar_open(conn, t, limit=10)])

print("\nBotar utan 'bot' i namnet")
for ua in ("Mozilla/5.0 (Linux; Android 6.0.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/W.X.Y.Z Mobile Safari/537.36 (compatible; Google-InspectionTool/1.0;)",
           "Mozilla/5.0 (compatible; GoogleOther)",
           "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ChatGPT-User/1.0; +https://openai.com/bot",
           "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)"):
    check(f"bot: {ua[:50]}…", looks_like_bot(ua))
for ua in ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"):
    check(f"människa: {ua[:50]}…", not looks_like_bot(ua))

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
