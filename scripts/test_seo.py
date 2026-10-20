#!/usr/bin/env python3
"""Every page says what it is about, to search engines and answer engines.

Until October 2026 all ~23,000 tender pages had the front page's meta
description and og:title, and the site-wide FAQ and app JSON-LD rode along on
every one of them. These cases are a tender page, a blog post, the front
page, the sitemap and llms.txt as a crawler sees them.

Run: python3 scripts/test_seo.py
"""
import json
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timezone

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import seo  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def tender(**kw):
    base = {"source_system": "mercell", "source_id": "1", "tender_url": "https://example.invalid",
            "title": "Byte av förångare - Bredstorps Idrottsplats", "authority": "Aktiebolaget Tranåsbostäder, TRANÅS",
            "cpv_codes": ["45331000"], "deadline": "2026-11-16T22:59:00Z", "published_at": "2026-10-14T05:42:59Z",
            "description": "", "value": None, "procedure": None, "contract_type": None,
            "document_type": None, "region": "Jönköpings län", "raw_json": "{}"}
    return {**base, **kw}


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

print("Titel och beskrivning per upphandling")
t = tender()
check("titeln har köparen, utan ort", seo.tender_title(t) == "Byte av förångare - Bredstorps Idrottsplats – Aktiebolaget Tranåsbostäder",
      seo.tender_title(t))
d = seo.tender_description(t, NOW)
check("beskrivningen säger vem, vad, när, var, källa",
      d == "Aktiebolaget Tranåsbostäder upphandlar Byte av förångare - Bredstorps Idrottsplats. "
           "Sista anbudsdag 16 november 2026. Jönköpings län. Källa: Mercell.", d)
check("23:59 svensk tid är rätt dag (22:59Z på vintern)",
      "16 november" in seo.tender_description(tender(deadline="2026-11-16T22:59:00Z"), NOW))
check("midnatt svensk tid är nästa dag, inte UTC-dagen",
      "17 november" in seo.tender_description(tender(deadline="2026-11-16T23:00:00Z"), NOW))
check("sommartid: 21:59Z i juni är 23:59 samma dag",
      "30 juni" in seo.tender_description(tender(deadline="2026-06-30T21:59:00Z"), NOW))
check("passerad deadline sägs i dåtid",
      "Sista anbudsdag var 1 oktober 2026" in seo.tender_description(tender(deadline="2026-10-01T10:00:00Z"), NOW))
check("tilldelning", seo.tender_description(tender(source_system="ted_awards"), NOW).startswith("Tilldelning:"))
check("…utan deadline", "anbudsdag" not in seo.tender_description(tender(source_system="ted_awards"), NOW))
check("förhandsinformation", seo.tender_description(tender(source_system="ted_pin"), NOW).startswith("Förhandsinformation:"))
long_t = tender(title="Ramavtal för service- och underhållsarbeten samt mindre ny-, till- och ombyggnadsarbeten "
                      "Bygg, El, Golv, Mark och VS inom kommunens samtliga fastigheter")
check("lång titel kortas vid ett ord", len(seo.tender_title(long_t)) <= 70 and seo.tender_title(long_t).endswith("…"),
      seo.tender_title(long_t))
check("…och beskrivningen håller sig kort", len(seo.tender_description(long_t, NOW)) <= 200,
      str(len(seo.tender_description(long_t, NOW))))
check("utan köpare och deadline går det ändå",
      seo.tender_description(tender(authority="", deadline=None, region=None), NOW)
      == "En offentlig köpare upphandlar Byte av förångare - Bredstorps Idrottsplats. Källa: Mercell.")
check("JSON-LD kan inte avsluta script-elementet",
      "</script>" not in seo.jsonld({"name": "x</script><script>alert(1)</script>"}))

print("\nSidorna")
DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
conn = connect(DB)
upsert_tender(conn, tender(title='Ventilation "Huset" </script><b>x</b>'))
conn.commit()
tid = conn.execute("SELECT id FROM tenders").fetchone()[0]
c = TestClient(APP, base_url="https://testserver")
h = c.get(f"/tenders/{tid}").text


def meta(html, attr, name):
    m_ = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', html)
    return m_.group(1) if m_ else None


def blocks(html):
    return [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


title = re.search(r"<title>(.*?)</title>", h, re.S).group(1)
check("upphandlingens <title> slutar med varumärket", title.endswith("| Agentanbud"), title)
check("meta description är upphandlingens egen", (meta(h, "name", "description") or "").startswith("Aktiebolaget Tranåsbostäder upphandlar"),
      meta(h, "name", "description"))
check("og:description likaså", meta(h, "property", "og:description") == meta(h, "name", "description"))
check("titeln escapas i attribut och <title>", "<b>x</b>" not in h.split("<body")[0] and "&lt;b&gt;" in title)
types = [b["@type"] for b in blocks(h)]
check("upphandlingen har BreadcrumbList, inte sajtens FAQ", types == ["BreadcrumbList"], types)
crumbs = blocks(h)[0]["itemListElement"]
check("brödsmulan pekar på sidan själv", crumbs[-1]["item"].endswith(f"/tenders/{tid}"))

home = c.get("/").text
check("startsidan har FAQ och app-beskrivning",
      sorted(b["@type"] for b in blocks(home)) == ["FAQPage", "WebApplication"], [b["@type"] for b in blocks(home)])
check("startsidans titel upprepar sig inte",
      re.search(r"<title>(.*?)</title>", home).group(1).count("Agentanbud") == 1)

c.post("/api/blog", headers={"x-admin-key": "k" * 64},
       json={"title": "VVS-utforskaren", "body_md": "Text.", "summary": "En agent och VVS.",
             "tags": ["VVS"], "image_url": "https://www.agentanbud.se/static/blog/vvs-utforskaren.png"})
bp = c.get("/blogg/vvs-utforskaren").text
post = [b for b in blocks(bp) if b["@type"] == "BlogPosting"]
check("blogginlägget har BlogPosting", len(post) == 1)
check("…med rubrik, datum, bild och utgivare",
      post and post[0]["headline"] == "VVS-utforskaren" and post[0].get("datePublished", "").endswith("Z")
      and post[0]["image"].endswith(".png") and post[0]["publisher"]["name"] == "Agentanbud", post)
check("…och en brödsmula via bloggen", any(b["@type"] == "BreadcrumbList" for b in blocks(bp)))

print("\nSitemap och llms.txt")
sm = c.get("/sitemap.xml").text
check("upphandlingen har lastmod = publiceringsdagen",
      f"/tenders/{tid}</loc><lastmod>2026-10-14</lastmod>" in sm)
upsert_tender(conn, tender(source_id="2", published_at="okänt"))
conn.commit()
check("ett konstigt datum ger ingen lastmod, inte en trasig",
      "<lastmod>okänt" not in c.get("/sitemap.xml").text)
llms = c.get("/llms.txt").text
check("llms.txt nämner alla källor",
      all(s in llms.split("## För AI-agenter")[0] for s in ("TED", "Mercell", "Kommersannons", "Upphandlingsmyndigheten")))

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
