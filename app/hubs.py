"""
Open tenders per industry and per county, as pages a search engine can index.

/browse?… is kept out of search engines (robots.txt), rightly: its filter
combinations are endless. That left no page that answers "open VVS tenders"
or "upphandlingar i Västra Götaland" — the questions people type and answer
engines quote. These pages are a fixed set, one per industry and one per
county, each with a short factual answer on top and the open tenders below,
rebuilt from the database on every request.

Industries are CPV prefixes. Counties use the region a notice states (Mercell
and Kommersannons name the county). TED notices give only "Sverige" and the
buyer's head-office town — a poor guide to where the work is, as the blog post
"Kartan vi inte publicerade" sets out — so county pages leave them out and say
so, rather than guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import seo


@dataclass(frozen=True)
class Hub:
    kind: str          # "bransch" | "lan"
    slug: str
    name: str          # "VVS, värme och ventilation" | "Stockholms län"
    phrase: str        # "inom VVS, värme och ventilation" | "i Stockholms län"
    match: tuple       # CPV prefixes | region substrings

    @property
    def path(self) -> str:
        return f"/upphandlingar/{self.kind}/{self.slug}"


def _sector(slug, name, *cpv):
    # "Bygg och anläggning" → "inom bygg och anläggning", but "VVS…" and
    # "IT-tjänster…" keep their capitals.
    lead = name if name[1:2].isupper() else name[0].lower() + name[1:]
    return Hub("bransch", slug, name, f"inom {lead}", cpv)


SECTORS = (
    _sector("bygg", "Bygg och anläggning", "45"),
    _sector("vvs", "VVS, värme och ventilation", "4533"),
    _sector("el", "El och belysning", "4531"),
    _sector("it", "IT-tjänster och programvara", "72", "48"),
    _sector("konsult", "Verksamhets- och managementkonsulter", "794"),
    _sector("bemanning", "Bemanning och rekrytering", "796"),
    _sector("arkitekt-teknik", "Arkitekt- och teknikkonsulter", "71"),
    _sector("juridik", "Juridiska tjänster", "791"),
    _sector("kommunikation", "Marknadsföring, kommunikation och tryck", "7934", "798"),
    _sector("vard", "Vård och omsorg", "85"),
    _sector("medicinteknik", "Medicinteknik och läkemedel", "33"),
    _sector("utbildning", "Utbildning", "80"),
    _sector("stad", "Städ och fastighetsservice", "909", "7033"),
    _sector("sakerhet", "Säkerhet och bevakning", "7971"),
    _sector("transport", "Transporter och skolskjuts", "60", "63"),
    _sector("fordon", "Fordon", "34"),
    _sector("livsmedel", "Livsmedel och måltider", "15", "5551", "5552"),
    _sector("energi", "Energi och bränsle", "09", "65"),
    _sector("avfall", "Avfall, vatten och avlopp", "904", "905"),
    _sector("mark-park", "Mark, park och skog", "77"),
    _sector("mobler", "Möbler och inredning", "39"),
    _sector("kontorsmaterial", "Kontorsmaterial", "3019"),
    _sector("telekom", "Telekom och post", "64"),
    _sector("forsakring-finans", "Försäkring och finans", "66"),
)

_COUNTIES = (
    ("blekinge", "Blekinge län"), ("dalarna", "Dalarnas län"), ("gotland", "Gotlands län"),
    ("gavleborg", "Gävleborgs län"), ("halland", "Hallands län"), ("jamtland", "Jämtlands län"),
    ("jonkoping", "Jönköpings län"), ("kalmar", "Kalmar län"), ("kronoberg", "Kronobergs län"),
    ("norrbotten", "Norrbottens län"), ("skane", "Skåne län"), ("stockholm", "Stockholms län"),
    ("sodermanland", "Södermanlands län"), ("uppsala", "Uppsala län"), ("varmland", "Värmlands län"),
    ("vasterbotten", "Västerbottens län"), ("vasternorrland", "Västernorrlands län"),
    ("vastmanland", "Västmanlands län"), ("vastra-gotaland", "Västra Götalands län"),
    ("orebro", "Örebro län"), ("ostergotland", "Östergötlands län"),
)
COUNTIES = tuple(Hub("lan", slug, name, f"i {name}", (name,)) for slug, name in _COUNTIES)

_BY_PATH = {(h.kind, h.slug): h for h in SECTORS + COUNTIES}

BUYER_TYPE_WORDS = {"municipal": "kommuner och kommunala bolag", "regional": "regioner",
                    "state": "statliga myndigheter", "unknown": "övriga"}


def find(kind: str, slug: str) -> Optional[Hub]:
    return _BY_PATH.get((kind, slug))


def _where(hub: Hub, now_iso: str) -> tuple[str, list]:
    """Open tenders in this hub: a deadline still ahead, award notices excluded."""
    where = ["source_system != 'ted_awards'", "deadline IS NOT NULL", "deadline > ?"]
    args: list = [now_iso]
    if hub.kind == "bransch":
        where.append("(" + " OR ".join("cpv_codes LIKE ?" for _ in hub.match) + ")")
        args += [f'%"{p}%' for p in hub.match]
    else:
        where.append("region LIKE ?")
        args.append(f"%{hub.match[0]}%")
    return " AND ".join(where), args


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def hub_page(conn, hub: Hub, now: Optional[datetime] = None, limit: int = 100) -> dict:
    """Everything the page shows: the facts for the answer, and the list."""
    now = now or datetime.now(timezone.utc)
    where, args = _where(hub, _iso(now))
    total = conn.execute(f"SELECT COUNT(*) FROM tenders WHERE {where}", args).fetchone()[0]
    soon_iso = _iso(now + timedelta(days=14))
    closing = conn.execute(f"SELECT COUNT(*) FROM tenders WHERE {where} AND deadline <= ?",
                           args + [soon_iso]).fetchone()[0]
    buyers = conn.execute(
        f"SELECT authority, COUNT(*) AS n FROM tenders WHERE {where} AND COALESCE(authority, '') != '' "
        f"GROUP BY authority ORDER BY n DESC, authority LIMIT 5", args).fetchall()
    kinds = conn.execute(
        f"SELECT COALESCE(buyer_type, 'unknown') AS k, COUNT(*) AS n FROM tenders WHERE {where} "
        f"GROUP BY k ORDER BY n DESC", args).fetchall()
    sources = [r[0] for r in conn.execute(
        f"SELECT source_system, COUNT(*) AS n FROM tenders WHERE {where} GROUP BY source_system "
        f"ORDER BY n DESC", args).fetchall()]
    rows = conn.execute(
        f"SELECT id, title, authority, region, deadline, source_system FROM tenders WHERE {where} "
        f"ORDER BY deadline ASC, id LIMIT ?", args + [limit]).fetchall()
    items = [{**dict(r), "deadline_text": seo._swedish_date(r["deadline"])} for r in rows]
    return {
        "hub": hub, "total": total, "closing_soon": closing,
        "buyers": [{"name": seo._buyer(r["authority"]) or r["authority"], "n": r["n"]} for r in buyers],
        "kinds": [{"label": BUYER_TYPE_WORDS.get(r["k"], "övriga"), "n": r["n"]} for r in kinds],
        "sources": list(dict.fromkeys(seo.SOURCE_NAMES.get(s, s) for s in sources)),
        "items": items, "shown": len(items),
    }


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " och " + words[-1]


def answer(page: dict, updated: Optional[str]) -> str:
    """The paragraph on top, which is also the FAQ answer: facts only."""
    hub, n = page["hub"], page["total"]
    if not n:
        return (f"Just nu finns inga öppna upphandlingar {hub.phrase} i Agentanbuds data. "
                f"Listan uppdateras varje morgon.")
    parts = [f"Just nu finns {n} öppna upphandlingar {hub.phrase} på Agentanbud."]
    if page["closing_soon"]:
        parts.append(f"{page['closing_soon']} av dem stänger inom två veckor.")
    if page["kinds"]:
        parts.append("Köparna är " + _join([f"{k['label']} ({k['n']})" for k in page["kinds"][:3]]) + ".")
    if page["buyers"] and page["buyers"][0]["n"] > 1:
        parts.append("Flest annonser just nu: "
                     + _join([f"{b['name']} ({b['n']})" for b in page["buyers"][:3] if b["n"] > 1]) + ".")
    if page["sources"]:
        parts.append("Källor: " + _join(page["sources"]) + ".")
    when = seo._swedish_date(updated)
    if when:
        parts.append(f"Uppdaterad {when}.")
    return " ".join(parts)


def page_jsonld(page: dict, text: str) -> list[str]:
    hub = page["hub"]
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question",
             "name": f"Hur många öppna upphandlingar finns {hub.phrase} just nu?",
             "acceptedAnswer": {"@type": "Answer", "text": text}},
            {"@type": "Question",
             "name": f"Var hittar jag offentliga upphandlingar {hub.phrase}?",
             "acceptedAnswer": {"@type": "Answer", "text":
                                f"Agentanbud listar alla öppna upphandlingar {hub.phrase} på "
                                f"{seo.SITE_URL}{hub.path}, uppdaterat varje dag. Samma data finns "
                                f"fritt via REST-API och MCP för AI-agenter."}},
        ],
    }
    crumbs = seo.breadcrumbs(("Agentanbud", "/"), ("Upphandlingar", "/upphandlingar"), (hub.name, hub.path))
    return [seo.jsonld(crumbs), seo.jsonld(faq)]


_INDEX_CACHE: dict = {}


def index_counts(conn, updated: Optional[str], now: Optional[datetime] = None) -> dict:
    """Open count per hub for /upphandlingar. 45 counts over the table cost a
    few hundred milliseconds, so they are kept until the data or the hour
    changes (deadlines pass during the day)."""
    now = now or datetime.now(timezone.utc)
    key = (updated, now.strftime("%Y-%m-%dT%H"))
    hit = _INDEX_CACHE.get("v")
    if hit and hit[0] == key:
        return hit[1]
    counts = {}
    for hub in SECTORS + COUNTIES:
        where, args = _where(hub, _iso(now))
        counts[hub.path] = conn.execute(f"SELECT COUNT(*) FROM tenders WHERE {where}", args).fetchone()[0]
    _INDEX_CACHE["v"] = (key, counts)
    return counts


def hubs_for_tender(t: dict) -> list[Hub]:
    """The industry and county pages a tender belongs to, for links from it."""
    out = []
    codes = t.get("cpv_codes_list") or []
    # Most specific industry first: VVS before Bygg for a 4533 code.
    for hub in sorted(SECTORS, key=lambda h: -max(len(p) for p in h.match)):
        if any(str(c).startswith(p) for c in codes for p in hub.match):
            out.append(hub)
            break
    region = t.get("region") or ""
    if t.get("source_system") not in ("ted", "ted_awards", "ted_pin"):
        for hub in COUNTIES:
            if hub.match[0] in region:
                out.append(hub)
                break
    return out

