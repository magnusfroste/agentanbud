"""
Mercell public search API client.

The endpoint below is unauthenticated and returns a JSON envelope with a
`results` array. We walk pages of recent Swedish tenders and upsert each
record into the local SQLite.

Verified live: ~525 SE records / 100 pages in ~80 seconds. The filter
syntax appears lossy (records ignore the filter and the same record
shows up across many pages) so we dedupe at the `(source_system,
source_id)` level via the unique index in schema.sql.

ID semantics (verified 2026-07-03 against app.mercell.com):
  - `id` is the search-index document id. The public tender page
    resolves ONLY by this id: /tender/{id} — both positive and negative
    ids work; repsNoticeId in the URL 404s. Mercell can re-index a
    notice under a NEW id, so `id` is not stable over time.
  - `repsNoticeId` is the stable notice number, present on all
    Mercell-native records. It is the right dedupe key when present.
  - `sourceId` == "TED" marks records Mercell mirrors from TED. We
    ingest TED directly (scraper/ted.py), so these are duplicates and
    are skipped. They also never carry a repsNoticeId.
"""
from __future__ import annotations

import html
import json
import logging
import os
import time
from typing import Iterator, Optional

import httpx

from app.db import connect, init_db, log_sync, upsert_tender

LOG = logging.getLogger(__name__)

API_BASE = "https://search-service-api.discover.app.mercell.com/public/api/v1/search"
DEFAULT_USER_AGENT = "agentanbud/0.1 (+https://github.com/magnusfroste/agentanbud)"
DEFAULT_MAX_PAGES = 100       # safety cap — gives ~10k records to walk
DEFAULT_PAGE_SIZE = 100
DEFAULT_REQUEST_DELAY_S = 0.4


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)


def _clean_description(raw: Optional[str]) -> str:
    if not raw:
        return ""
    return html.unescape(raw).replace("\r\n", "\n").replace("\r", "\n").strip()


def _mercell_slug(title: str) -> str:
    """Build a Mercell-style URL slug from a title.

    Mercell slugs are lowercase ASCII with - separators and Swedish
    characters folded (a, a, o, etc). This is best-effort cosmetic —
    Mercell ignores the slug in routing, it's purely for SEO/share-link
    readability.
    """
    if not title:
        return ""
    s = title.lower()
    # Fold Swedish/diacritics to ASCII
    s = (s.replace("å", "a").replace("ä", "a").replace("ö", "o")
           .replace("é", "e").replace("è", "e").replace("ü", "u")
           .replace("á", "a").replace("à", "a").replace("í", "i"))
    import re
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def _mercell_url(rec: dict) -> str:
    """Build the public Mercell URL for one tender.

    The page resolves by the search-index `id` (positive or negative) —
    NOT by repsNoticeId, which 404s in the URL. The slug is cosmetic;
    Mercell routes on the id alone. The old /sv-SE/m/tender/{id} path is
    a dead legacy route (404s), never emit it.
    """
    tender_id = rec.get("id")
    if not tender_id:
        return ""
    slug = _mercell_slug(rec.get("title", ""))
    if slug:
        return f"https://app.mercell.com/tender/{tender_id}/{slug}"
    return f"https://app.mercell.com/tender/{tender_id}"


def _map_record(rec: dict) -> dict:
    """Translate one Mercell record to a `tenders` row dict.

    source_id is repsNoticeId when present — the stable notice number.
    Mercell re-indexes notices under new search ids over time, so keying
    on `id` creates duplicate rows for the same notice. Records without
    repsNoticeId (rare, non-TED) fall back to the search id.
    """
    cpv = rec.get("cpvCodes") or []
    if not isinstance(cpv, list):
        cpv = [str(cpv)]
    return {
        "source_system": "mercell",
        "source_id": str(rec.get("repsNoticeId") or rec.get("id", "")),
        "tender_url": _mercell_url(rec),
        "title": (rec.get("title") or "").strip(),
        "authority": (rec.get("authorityTown") or "").strip(),
        "cpv_codes": json.dumps(cpv, ensure_ascii=False),
        "deadline": rec.get("deadline"),
        "published_at": rec.get("publicationDate"),
        "description": _clean_description(rec.get("description")),
        "value": None,  # Mercell API does not expose value directly
        "procedure": rec.get("docTypeCode"),
        "contract_type": rec.get("marketType"),
        "document_type": rec.get("documentCategory"),
        "region": (rec.get("deliveryPlaceNames") or [""])[0] or None,
        "raw_json": json.dumps(rec, ensure_ascii=False),
    }


def _walk_pages(
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_size: int = DEFAULT_PAGE_SIZE,
    delay_s: float = DEFAULT_REQUEST_DELAY_S,
) -> Iterator[dict]:
    """Yield Mercell records, paginating until empty page or max_pages."""
    ua = _user_agent()
    seen: set[str] = set()
    with httpx.Client(
        headers={"User-Agent": ua, "Accept": "application/json"},
        timeout=20,
        follow_redirects=True,
    ) as client:
        for page in range(1, max_pages + 1):
            try:
                resp = client.get(API_BASE, params={"page": str(page), "pageSize": str(page_size)})
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "30"))
                    LOG.warning("429 from Mercell, sleeping %ds", retry_after)
                    time.sleep(retry_after)
                    resp = client.get(API_BASE, params={"page": str(page), "pageSize": str(page_size)})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                LOG.warning("mercell page %d fetch failed: %s", page, exc)
                return
            results = data.get("results") or []
            if not results:
                LOG.info("mercell page %d empty, stopping", page)
                return
            for rec in results:
                rid = str(rec.get("id", ""))
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                # Only Swedish records
                if rec.get("authorityCountryCode") != "SE":
                    continue
                # Skip Mercell's TED mirrors — we ingest TED directly
                # (scraper/ted.py), so these only create duplicates.
                if rec.get("sourceId") == "TED":
                    continue
                yield rec
            time.sleep(delay_s)


def _dedupe_stale(records: list[dict]) -> list[dict]:
    """Drop stale index entries that lack repsNoticeId when a record
    with repsNoticeId exists for the same (title, authority). Mercell
    keeps old index documents around after re-indexing; the reps-less
    copy is the stale one."""
    def key(rec: dict) -> tuple:
        title = (rec.get("title") or "").strip().lower()
        auth = (rec.get("authorityTown") or "").split(",")[0].strip().lower()
        return (title, auth)

    with_reps = {key(r) for r in records if r.get("repsNoticeId")}
    kept = [r for r in records if r.get("repsNoticeId") or key(r) not in with_reps]
    dropped = len(records) - len(kept)
    if dropped:
        LOG.info("mercell: dropped %d stale reps-less duplicates", dropped)
    return kept


def run(db_path: str) -> int:
    """Walk Mercell, upsert everything, log the run. Returns rows written."""
    init_db(db_path)
    conn = connect(db_path)
    written = 0
    try:
        for rec in _dedupe_stale(list(_walk_pages())):
            try:
                upsert_tender(conn, _map_record(rec))
                written += 1
            except Exception as exc:
                LOG.warning("mercell record %r failed: %s", rec.get("id"), exc)
        conn.commit()
        log_sync(conn, source="mercell", status="ok", count=written,
                 message=f"walked up to {DEFAULT_MAX_PAGES} pages")
        LOG.info("mercell: wrote/updated %d tenders", written)
        return written
    except Exception as exc:
        conn.rollback()
        log_sync(conn, source="mercell", status="error", count=written, message=str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run(os.environ.get("DB_PATH", "/data/application.db"))
