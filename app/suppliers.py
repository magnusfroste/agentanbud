"""
Supplier pages: /leverantor and /leverantor/{name-orgnr}.

The buyer pages answer "who wins here?". These answer the question from the
other side, the one a supplier asks about a competitor and an answer engine
is asked about a company: what has it won, from whom, and in what? From the
contracts table (scraper/uhm_contracts.py, Upphandlingsmyndigheten's open
statistics): every registered advertising database, below the EU thresholds
too, 2021 onwards.

Only a Swedish legal entity gets a page, and only it has its number shown:
ten digits, a third digit of 2 or more and a valid check digit. A sole
trader's organisation number is the owner's personal identity number, whose
third digit is the first of the birth month (0 or 1). The agency anonymises
sole traders, but this does not rely on that: a number that could be a
person's is never published here. The rows that fail the test are foreign
registrations and placeholders ("SEKRETESS", "FLERA KONTRAKT…").

Performance: a page is a handful of queries on one supplier's rows, by the
supplier_key index. The index page and the sitemap need every supplier's
count, a scan of the table; that list is kept in memory and rebuilt only
when a new file has been imported (source_state), which is once a year.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections import Counter
from typing import Optional

from app import seo

PAGE_MIN_CONTRACTS = 5
INDEX_SIZE = 300          # the most frequent winners on /leverantor; the rest via the sitemap
_SLUG_TAIL = re.compile(r"(?:^|-)(\d{10})$")


def is_company_number(orgnr: Optional[str]) -> bool:
    """A Swedish legal entity's organisation number, never a person's."""
    n = orgnr or ""
    return len(n) == 10 and n.isdigit() and n[2] >= "2" and _luhn_ok(n)


def could_be_person(orgnr: Optional[str]) -> bool:
    """True for a number that could be a Swedish personal identity number
    (or co-ordination number): ten digits, a valid month and day — 61–91 for
    a co-ordination number — and a valid check digit. A sole trader's
    organisation number is exactly that."""
    n = orgnr or ""
    if len(n) != 10 or not n.isdigit() or not _luhn_ok(n):
        return False
    month, day = int(n[2:4]), int(n[4:6])
    return 1 <= month <= 12 and (1 <= day <= 31 or 61 <= day <= 91)


def _luhn_ok(n: str) -> bool:
    total = 0
    for i, ch in enumerate(n):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def supplier_slug(name: str, orgnr: str) -> str:
    from app.related import slugify
    return f"{slugify(name)}-{orgnr}".strip("-")


def orgnr_from_slug(slug: str) -> Optional[str]:
    m = _SLUG_TAIL.search(slug or "")
    return m.group(1) if m else None


def _version(conn):
    try:
        r = conn.execute("SELECT value, updated_at FROM source_state WHERE source = 'uhm_contracts'").fetchone()
    except sqlite3.OperationalError:
        return None
    return tuple(r) if r else None


_LOCK = threading.Lock()
_LISTS: dict = {}     # database path -> (version, [entries], {orgnr: entry})


def _all(conn) -> tuple[list, dict]:
    """Every supplier with a page, most contracts first."""
    from mcp_shared import pretty_company
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    version = _version(conn)
    with _LOCK:
        hit = _LISTS.get(path)
        if hit and hit[0] == version and version is not None:
            return hit[1], hit[2]
    try:
        rows = conn.execute(
            "SELECT supplier_key, MAX(supplier_name), COUNT(*) AS n, COUNT(DISTINCT buyer_key) "
            "FROM contracts WHERE supplier_name IS NOT NULL GROUP BY supplier_key "
            "HAVING n >= ? ORDER BY n DESC, 2", (PAGE_MIN_CONTRACTS,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    entries = []
    for key, name, n, buyers in rows:
        if not is_company_number(key):
            continue
        label = pretty_company(name)
        entries.append({"orgnr": key, "name": label, "slug": supplier_slug(label, key),
                        "n": n, "buyers": buyers})
    by_orgnr = {e["orgnr"]: e for e in entries}
    with _LOCK:
        _LISTS[path] = (version, entries, by_orgnr)
    return entries, by_orgnr


def warm(path: str) -> threading.Thread:
    """Build the supplier list in the background after a start (0.3 s)."""
    def run():
        from app.db import connect
        try:
            conn = connect(path)
            try:
                _all(conn)
            finally:
                conn.close()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("supplier list warm-up failed")
    t = threading.Thread(target=run, daemon=True, name="suppliers-warm")
    t.start()
    return t


def reset() -> None:
    with _LOCK:
        _LISTS.clear()


def page_slug(conn, orgnr: Optional[str]) -> Optional[str]:
    """The page for this supplier, if it has one."""
    if not is_company_number(orgnr):
        return None
    e = _all(conn)[1].get(orgnr)
    return e["slug"] if e else None


def slugs(conn) -> list[str]:
    return [e["slug"] for e in _all(conn)[0]]


def index(conn) -> list[dict]:
    """The suppliers with the most contracts, for /leverantor."""
    return _all(conn)[0][:INDEX_SIZE]


def count(conn) -> int:
    return len(_all(conn)[0])


def supplier_page(conn, orgnr: str) -> Optional[dict]:
    """Everything a supplier's page shows."""
    from app import related
    from mcp_shared import SME_SIZES, pretty_company
    entry = _all(conn)[1].get(orgnr) if is_company_number(orgnr) else None
    if entry is None:
        return None
    rows = conn.execute(
        "SELECT title, buyer_name, procurement_id, cpv, published, year, kind, above_threshold, "
        "appealed, supplier_size FROM contracts WHERE supplier_key = ? ORDER BY published DESC",
        (orgnr,)).fetchall()
    if not rows:
        return None
    buyers = Counter(r["buyer_name"] for r in rows if r["buyer_name"])
    divisions = Counter(r["cpv"][:2] for r in rows if r["cpv"])
    sizes = Counter(r["supplier_size"] for r in rows if r["supplier_size"])
    years = [r["year"] for r in rows if r["year"]]
    procs = {r["procurement_id"] for r in rows if r["procurement_id"]}
    known = [r for r in rows if r["above_threshold"] is not None]

    top_buyers = []
    for name, n in buyers.most_common(10):
        ref = related.buyer_ref(conn, name)
        top_buyers.append({"name": ref["name"] if ref else pretty_company(name),
                           "slug": ref["slug"] if ref else None, "n": n})
    usual = []
    for d, n in divisions.most_common(5):
        hub = related._sector_for_division(d)
        usual.append({"code": d, "label": related.cpv_label(d + "000000") or f"CPV {d}", "n": n,
                      "hub": hub.path if hub else None})
    recent, seen = [], set()
    for r in rows:
        if r["procurement_id"] in seen:
            continue
        seen.add(r["procurement_id"])
        ref = related.buyer_ref(conn, r["buyer_name"]) if r["buyer_name"] else None
        recent.append({"title": r["title"] or "(utan titel)",
                       "buyer": ref["name"] if ref else pretty_company(r["buyer_name"] or ""),
                       "buyer_slug": ref["slug"] if ref else None,
                       "date": seo._swedish_date(r["published"]) if r["published"] else str(r["year"] or ""),
                       "kind": r["kind"]})
        if len(recent) >= 15:
            break
    size = sizes.most_common(1)[0][0] if sizes else None

    def pct(a, b):
        return round(100 * a / b) if b else None
    return {
        "name": entry["name"], "orgnr": orgnr, "slug": entry["slug"],
        "size": size, "sme": size in SME_SIZES if size else None,
        "contracts": len(rows), "procurements": len(procs), "buyer_count": len(buyers),
        "years": (f"{min(years)}–{max(years)}" if min(years) != max(years) else str(min(years))) if years else "",
        "frameworks_pct": pct(sum(1 for r in rows if r["kind"] == "Ramavtal"), len(rows)),
        "below_threshold_pct": pct(sum(1 for r in known if r["above_threshold"] == 0), len(known)),
        "appealed_pct": pct(sum(1 for r in rows if r["appealed"] == 1), len(rows)),
        "buyers": top_buyers, "usual": usual, "recent": recent,
    }


def supplier_answer(page: dict) -> str:
    """The paragraph on top of a supplier page, also its description and FAQ answer."""
    parts = [f"{page['name']} tilldelades {page['contracts']} kontrakt i {page['procurements']} "
             f"offentliga upphandling{'ar' if page['procurements'] != 1 else ''} från "
             f"{page['buyer_count']} köpare {page['years']}, enligt Upphandlingsmyndighetens statistik."]
    if page["buyers"]:
        parts.append("Flest från " + ", ".join(f"{b['name']} ({b['n']})" for b in page["buyers"][:3]) + ".")
    if page["usual"]:
        parts.append("Vinner oftast inom: " + "; ".join(
            u["label"] if u["label"][1:2].isupper() else u["label"][:1].lower() + u["label"][1:]
            for u in page["usual"][:3]) + ".")
    return " ".join(parts)


def supplier_jsonld(page: dict, text: str) -> list[str]:
    path = f"/leverantor/{page['slug']}"
    org = {"@context": "https://schema.org", "@type": "Organization", "name": page["name"],
           "taxID": page["orgnr"], "url": seo.SITE_URL + path}
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f"Vilka offentliga upphandlingar har {page['name']} vunnit?",
         "acceptedAnswer": {"@type": "Answer", "text": text}}]}
    crumbs = seo.breadcrumbs(("Agentanbud", "/"), ("Leverantörer", "/leverantor"), (page["name"], path))
    return [seo.jsonld(crumbs), seo.jsonld(org), seo.jsonld(faq)]


def find(conn, query: str, limit: int = 5) -> list[dict]:
    """Suppliers with a page whose name contains the query, or with this number."""
    entries, by_orgnr = _all(conn)
    digits = re.sub(r"\D", "", query or "")
    if len(digits) == 10:
        return [by_orgnr[digits]] if digits in by_orgnr else []
    q = (query or "").strip().casefold()
    if len(q) < 2:
        return []
    return [e for e in entries if q in e["name"].casefold()][:limit]
