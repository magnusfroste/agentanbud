"""
What a tender page adds to the notice: CPV codes in words, similar open
tenders, and what we know about the buyer.

A tender page used to show what the source shows — title, dates, description,
bare CPV numbers — so to a reader and to a search engine it was a copy of the
notice. The MCP tools already answered the questions a supplier asks next
(similar_tenders, get_authority, get_winner_history); this brings the same
answers to the page, from the same data.

Performance: everything here is answered from an in-memory snapshot of the
table, built in one pass and rebuilt in the background when the data changes
(at most once a minute) or every hour. The first version queried the
database on each page view — similar tenders by LIKE over every row's CPV
list, the buyer by scanning its rows — and on 24,000 tenders that took a page
from 6 ms to 41 ms and sixteen concurrent page views from 179 pages a second
to 20. A crawler reads tender pages sixteen at a time. From the snapshot a
page costs about what it did before.

CPV labels are the EU's own Swedish names (CPV 2008, 9,454 codes), fetched
from the Publications Office (data.europa.eu/cpv) into app/vocab/cpv_sv.json (not
"data/", which git and the Docker build ignore: that is where the database
lives).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app import seo

LOG = logging.getLogger(__name__)

CPV_FILE = Path(__file__).parent / "vocab" / "cpv_sv.json"


@lru_cache(maxsize=1)
def _labels() -> dict:
    try:
        return json.loads(CPV_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cpv_label(code: str) -> Optional[str]:
    """The Swedish name of a CPV code, or of its nearest parent.

    Sources drop the check digit and sometimes use codes finer than the
    vocabulary; zeroing digits from the right walks up the tree
    (45331100 → 45331000 → 45330000 …) until a named code is found.
    """
    code = "".join(ch for ch in str(code or "") if ch.isdigit())[:8].ljust(8, "0")
    if len(code) != 8 or code == "00000000":
        return None
    labels = _labels()
    for keep in range(8, 1, -1):
        candidate = code[:keep].ljust(8, "0")
        if candidate in labels:
            return labels[candidate]
    return None


def cpv_list(codes) -> list[dict]:
    return [{"code": str(c), "label": cpv_label(c)} for c in (codes or [])]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


PAGE_MIN_NOTICES = 5   # a buyer gets a page at five notices or five awards

_SLUG_MAP = str.maketrans({"å": "a", "ä": "a", "ö": "o", "é": "e", "ü": "u", "æ": "ae", "ø": "o"})


def slugify(name: str) -> str:
    """"Västra Götalandsregionen" → "vastra-gotalandsregionen"."""
    import re
    s = (name or "").lower().translate(_SLUG_MAP)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:80]


class _Snapshot:
    """Open tenders by CPV group and by buyer, and a profile per buyer.

    Immutable once built; a rebuild replaces it whole. Deadlines are kept, not
    an "open" flag, so a tender that closes during the day drops out at once.
    """

    def __init__(self, conn, version):
        self.version, self.built = version, time.monotonic()
        now = _now_iso()
        self.items: dict = {}          # id -> (title, authority, deadline, region, cpv set, date text)
        self.by_group: dict = {}       # first four CPV digits -> [ids]
        self.by_authority: dict = {}   # authority exactly as written -> [ids]
        self.buyers: dict = {}         # lower-cased name without town -> profile parts
        for r in conn.execute("SELECT id, source_system, title, authority, deadline, region, cpv_codes FROM tenders"):
            try:
                codes = {str(c) for c in json.loads(r["cpv_codes"] or "[]") if c}
            except (TypeError, ValueError):
                codes = set()
            key = seo._buyer(r["authority"]).lower()
            if key:
                b = self.buyers.setdefault(key, {"names": Counter(), "deadlines": [], "open_ids": [],
                                                 "notices": 0, "divisions": Counter(),
                                                 "wins": Counter(), "contracts": 0,
                                                 "nat_wins": Counter(), "nat": None})
                b["names"][seo._buyer(r["authority"])] += 1
                b["divisions"].update({c[:2] for c in codes})
                if r["source_system"] != "ted_awards":
                    b["notices"] += 1
                    if r["deadline"]:
                        b["deadlines"].append(r["deadline"])
            if r["source_system"] == "ted_awards" or not r["deadline"] or r["deadline"] <= now:
                continue
            self.items[r["id"]] = (r["title"], r["authority"], r["deadline"], r["region"], codes,
                                   seo._swedish_date(r["deadline"]))
            for g in {c[:4] for c in codes if len(c) >= 4}:
                self.by_group.setdefault(g, []).append(r["id"])
            if r["authority"]:
                self.by_authority.setdefault(r["authority"].strip(), []).append(r["id"])
            if key:
                self.buyers[key]["open_ids"].append(r["id"])
        # Who each buyer awarded contracts to, counted as get_winner_history
        # counts: TED republications collapsed, spellings of one supplier merged.
        from mcp_shared import AWARD_DEDUP_KEY, canonical_supplier
        spellings: dict = {}
        for authority, winners_json in conn.execute(
                "SELECT authority, winner_name FROM tenders WHERE source_system = 'ted_awards' "
                f"AND winner_name IS NOT NULL AND winner_name != '' {AWARD_DEDUP_KEY}"):
            b = self.buyers.get(seo._buyer(authority).lower())
            try:
                winners = json.loads(winners_json)
            except (TypeError, ValueError):
                continue
            if b is None or not winners:
                continue
            b["contracts"] += 1
            for w in winners:
                k = canonical_supplier(w)
                if k:
                    b["wins"][k] += 1
                    spellings.setdefault(k, Counter())[w] += 1
        self.label = {k: c.most_common(1)[0][0] for k, c in spellings.items()}
        # Contracts awarded in Sweden (Upphandlingsmyndigheten, scraper/
        # uhm_contracts.py): every database and below the thresholds too,
        # matched to a buyer by its name. Grouped in SQLite, so the 100,000
        # rows are one pass there and a few thousand groups here.
        self.nat_label: dict = {}
        self._add_contracts(conn)
        # A display name per buyer (the spelling seen most, preferring one
        # that is not all capitals) and a page for the buyers with enough to
        # say: a page with one notice on it is thin content, not an answer.
        self.slugs: dict = {}
        for key, b in sorted(self.buyers.items(), key=lambda kv: -kv[1]["notices"]):
            names = b["names"].most_common()
            b["name"] = next((n for n, _ in names if not n.isupper()), names[0][0])
            b["slug"] = None
            if (b["notices"] >= PAGE_MIN_NOTICES or b["contracts"] >= PAGE_MIN_NOTICES
                    or (b["nat"] and b["nat"]["contracts"] >= PAGE_MIN_NOTICES)):
                slug = slugify(b["name"])
                if slug and slug not in self.slugs:
                    self.slugs[slug] = key
                    b["slug"] = slug


    def _add_contracts(self, conn) -> None:
        by_match: dict = {}
        for key in self.buyers:
            by_match.setdefault(buyer_match_key(key), key)

        def find(buyer: str):
            key = by_match.get(buyer_match_key(seo._buyer(buyer)))
            return self.buyers.get(key) if key else None

        import sqlite3
        from mcp_shared import SME_SIZES, pretty_company
        try:
            totals = conn.execute(
                "SELECT buyer_name, COUNT(*), COUNT(DISTINCT procurement_id), MIN(year), MAX(year), "
                "SUM(supplier_size IN (?, ?, ?)), SUM(supplier_size IS NOT NULL) "
                "FROM contracts WHERE buyer_name IS NOT NULL GROUP BY buyer_name", SME_SIZES).fetchall()
            pairs = conn.execute(
                "SELECT buyer_name, supplier_key, MAX(supplier_name), COUNT(*) FROM contracts "
                "WHERE buyer_name IS NOT NULL AND supplier_name IS NOT NULL "
                "GROUP BY buyer_name, supplier_key").fetchall()
        except sqlite3.OperationalError:      # no contracts table yet
            return
        for buyer, n, procs, first, last, sme, sized in totals:
            b = find(buyer)
            if b is None:
                continue
            nat = b["nat"] or {"contracts": 0, "procurements": 0, "first": first, "last": last,
                               "sme": 0, "sized": 0}
            nat["contracts"] += n
            nat["procurements"] += procs
            nat["first"], nat["last"] = min(nat["first"], first), max(nat["last"], last)
            nat["sme"] += sme or 0
            nat["sized"] += sized or 0
            b["nat"] = nat
        for buyer, key, name, n in pairs:
            b = find(buyer)
            if b is None:
                continue
            b["nat_wins"][key] += n
            if key not in self.nat_label:
                self.nat_label[key] = pretty_company(name)


def buyer_match_key(name: str) -> str:
    """A buyer's name as the sources and Upphandlingsmyndigheten both write it.

    The agency uses the registered name in capitals — "GÖTEBORGS KOMMUN",
    "BORLÄNGE ENERGI AB" — where the notices say "Göteborgs Stad" and "AB
    Borlänge Energi". Case, the company form wherever it stands, punctuation,
    and "stad" for "kommun" (the cities that call themselves one) are set
    aside; nothing else is guessed. 498 of the 609 buyer pages matched this
    way in October 2026, against 459 on the exact name.
    """
    import re
    n = (name or "").casefold()
    n = re.sub(r"\b(aktiebolaget|aktiebolag|ab|publ)\b", " ", n)
    n = re.sub(r"[.,()/&-]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n[:-4] + "kommun" if n.endswith(" stad") else n


def _national(snap, b: dict, top: int) -> Optional[dict]:
    """A buyer's contracts in Upphandlingsmyndigheten's statistics, if any."""
    nat = b.get("nat")
    if not nat or not nat["contracts"]:
        return None
    return {"contracts": nat["contracts"], "procurements": nat["procurements"],
            "years": f"{nat['first']}–{nat['last']}" if nat["first"] != nat["last"] else str(nat["first"]),
            "sme_pct": round(100 * nat["sme"] / nat["sized"]) if nat["sized"] else None,
            "winners": [{"name": snap.nat_label.get(k, k), "n": n} for k, n in b["nat_wins"].most_common(top)]}


_SNAPSHOTS: dict = {}
_LOCK = threading.Lock()
_BUILDING: set = set()
FRESH_S = 3600       # rebuilt in the background every hour regardless (deadlines
                     # are checked at query time, so this is only a backstop)
MIN_REBUILD_S = 60   # and when the data changed, but not more than once a minute


def _version(conn) -> tuple:
    return tuple(conn.execute("SELECT COUNT(*), MAX(id) FROM tenders").fetchone())


def _path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def _rebuild(path: str) -> None:
    from app.db import connect
    try:
        conn = connect(path)
        try:
            snap = _Snapshot(conn, _version(conn))
        finally:
            conn.close()
        with _LOCK:
            _SNAPSHOTS[path] = snap
    except Exception:
        LOG.exception("related snapshot rebuild failed")
    finally:
        with _LOCK:
            _BUILDING.discard(path)


def _snapshot(conn) -> _Snapshot:
    """The current snapshot; the first one is built in the request, later ones
    in the background while the old one keeps answering."""
    path = _path(conn)
    with _LOCK:
        snap = _SNAPSHOTS.get(path)
    if snap is None:
        with _LOCK:
            snap = _SNAPSHOTS.get(path)
            if snap is None:
                snap = _SNAPSHOTS[path] = _Snapshot(conn, _version(conn))
        return snap
    age = time.monotonic() - snap.built
    if age >= FRESH_S or (age >= MIN_REBUILD_S and _version(conn) != snap.version):
        with _LOCK:
            if path not in _BUILDING:
                _BUILDING.add(path)
                threading.Thread(target=_rebuild, args=(path,), daemon=True, name="related-rebuild").start()
    return snap


def warm(path: str) -> threading.Thread:
    """Build the first snapshot in the background, so the first tender page
    after a start does not pay for it (about 0.3 s at 24,000 tenders)."""
    with _LOCK:
        _BUILDING.add(path)
    t = threading.Thread(target=_rebuild, args=(path,), daemon=True, name="related-warm")
    t.start()
    return t


def reset() -> None:
    """Forget every snapshot (tests change the data and look again at once)."""
    with _LOCK:
        _SNAPSHOTS.clear()


def similar_open(conn, t: dict, limit: int = 5) -> list[dict]:
    """Open tenders sharing a CPV group (first four digits) or the buyer.

    Scored as the MCP tool similar_tenders does: two points per shared CPV
    code, three for the same buyer; soonest deadline breaks ties.
    """
    snap = _snapshot(conn)
    codes = {str(c) for c in (t.get("cpv_codes_list") or [])}
    buyer = (t.get("authority") or "").strip()
    candidates = set(snap.by_authority.get(buyer, ())) if buyer else set()
    for g in {c[:4] for c in codes if len(c) >= 4}:
        candidates.update(snap.by_group.get(g, ()))
    candidates.discard(t.get("id"))
    now = _now_iso()
    scored = []
    for i in candidates:
        title, authority, deadline, region, theirs, _ = snap.items[i]
        if deadline <= now:
            continue
        score = 2 * len(codes & theirs) + (3 if buyer and authority == buyer else 0)
        if score:
            scored.append((-score, deadline, i))
    scored.sort()
    out = []
    for _, deadline, i in scored[:limit]:
        title, authority, _, region, _, when = snap.items[i]
        out.append({"id": i, "title": title, "authority": authority, "region": region,
                    "deadline_text": when})
    return out


def buyer_profile(conn, authority: Optional[str]) -> Optional[dict]:
    """Open notices, usual industries and usual winners for this buyer.

    The buyer is its name as the sources write it, with or without the
    ", TOWN" Mercell and TED append and in any case — "Region Halland",
    "REGION HALLAND" and "Region Halland, HALMSTAD" are one buyer — never a
    substring, which for a name like "Region" would mean every region.
    """
    name = seo._buyer(authority)
    if len(name) < 4:
        return None
    b = _snapshot(conn).buyers.get(name.lower())
    if b is None:
        return None
    snap = _snapshot(conn)
    now = _now_iso()
    return {
        "name": b.get("name") or name,
        "slug": b.get("slug"),
        "open": sum(1 for d in b["deadlines"] if d > now),
        "notices": b["notices"],
        "usual": [{"code": d, "label": cpv_label(d + "000000") or f"CPV {d}", "n": n}
                  for d, n in b["divisions"].most_common(3)],
        "winners": [{"name": snap.label.get(k, k), "n": n} for k, n in b["wins"].most_common(3)],
        "contracts": b["contracts"],
        "national": _national(snap, b, 3),
    }


# ---------------------------------------------------------------------------
# Buyer pages: /kopare and /kopare/{slug}
# ---------------------------------------------------------------------------
def _sector_for_division(division: str):
    """The industry page (app/hubs.py) whose CPV prefix is this division, if any."""
    from app import hubs
    for hub in hubs.SECTORS:
        if division in hub.match:
            return hub
    return None


def buyer_page(conn, slug: str) -> Optional[dict]:
    """Everything a buyer's page shows, from the snapshot."""
    snap = _snapshot(conn)
    key = snap.slugs.get(slug)
    if key is None:
        return None
    b = snap.buyers[key]
    now = _now_iso()
    open_items = sorted((snap.items[i] + (i,) for i in b["open_ids"] if snap.items[i][2] > now),
                        key=lambda x: x[2])
    usual = []
    for d, n in b["divisions"].most_common(5):
        hub = _sector_for_division(d)
        usual.append({"code": d, "label": cpv_label(d + "000000") or f"CPV {d}", "n": n,
                      "hub": hub.path if hub else None})
    return {
        "name": b["name"], "slug": slug, "notices": b["notices"], "contracts": b["contracts"],
        "open": [{"id": x[6], "title": x[0], "deadline_text": x[5], "region": x[3]} for x in open_items],
        "usual": usual,
        "winners": [{"name": snap.label.get(k, k), "n": n} for k, n in b["wins"].most_common(10)],
        "national": _national(snap, b, 10),
    }


def buyer_answer(page: dict) -> str:
    """The paragraph on top of a buyer page, also its description and FAQ answer."""
    name, n = page["name"], len(page["open"])
    parts = [f"{name} har {n} öppna upphandling{'ar' if n != 1 else ''} på Agentanbud just nu "
             f"och {page['notices']} annons{'er' if page['notices'] != 1 else ''} i vår data."]
    if page["usual"]:
        # Semicolons: CPV names have commas of their own.
        parts.append("Köper oftast: " + "; ".join(
            u["label"] if u["label"][1:2].isupper() else u["label"][:1].lower() + u["label"][1:]
            for u in page["usual"][:3]) + ".")
    nat = page.get("national")
    if nat and nat["winners"]:
        top = ", ".join(f"{w['name']} ({w['n']})" for w in nat["winners"][:3])
        parts.append(f"{nat['years']} gick flest kontrakt till {top}, av {nat['contracts']} "
                     f"kontrakterade anbud i Upphandlingsmyndighetens statistik.")
    elif page["winners"]:
        top = ", ".join(f"{w['name']} ({w['n']})" for w in page["winners"][:3])
        parts.append(f"Har oftast tilldelat kontrakt till {top}, enligt {page['contracts']} "
                     f"tilldelning{'ar' if page['contracts'] != 1 else ''} i TED.")
    return " ".join(parts)


def buyer_jsonld(page: dict, text: str) -> list[str]:
    path = f"/kopare/{page['slug']}"
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f"Vad upphandlar {page['name']}?",
         "acceptedAnswer": {"@type": "Answer", "text": text}}]}
    nat = page.get("national")
    answers = []
    if nat and nat["winners"]:
        answers.append(f"Enligt Upphandlingsmyndighetens statistik över tilldelade kontrakt {nat['years']}: "
                       + ", ".join(f"{w['name']} ({w['n']})" for w in nat["winners"][:5]) + ".")
    if page["winners"]:
        answers.append("Enligt tilldelningar publicerade i TED: "
                       + ", ".join(f"{w['name']} ({w['n']})" for w in page["winners"][:5]) + ".")
    if answers:
        faq["mainEntity"].append({
            "@type": "Question", "name": f"Vem vinner upphandlingar hos {page['name']}?",
            "acceptedAnswer": {"@type": "Answer", "text": " ".join(answers)}})
    crumbs = seo.breadcrumbs(("Agentanbud", "/"), ("Köpare", "/kopare"), (page["name"], path))
    return [seo.jsonld(crumbs), seo.jsonld(faq)]


_SV_ORDER = str.maketrans({"å": "{", "ä": "|", "ö": "}"})   # å, ä, ö after z, in that order


def buyers_index(conn) -> list[dict]:
    """Every buyer with a page, in Swedish alphabetical order."""
    snap = _snapshot(conn)
    now = _now_iso()
    out = []
    for slug, key in snap.slugs.items():
        b = snap.buyers[key]
        out.append({"name": b["name"], "slug": slug, "notices": b["notices"],
                    "open": sum(1 for d in b["deadlines"] if d > now)})
    out.sort(key=lambda x: x["name"].lower().translate(_SV_ORDER))
    return out


def buyer_slugs(conn) -> list[str]:
    return list(_snapshot(conn).slugs)
