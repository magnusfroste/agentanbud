"""
Mercell public search API client.

The endpoint below is unauthenticated and returns a JSON envelope with a
`results` array. We walk pages of recent Swedish tenders and upsert each
record into the local SQLite.

Verified live: ~525 SE records / 100 pages in ~80 seconds. The filter
syntax appears lossy (records ignore the filter and the same record
shows up across many pages) so we dedupe at the `(source_system,
source_id)` level via the unique index in schema.sql.
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


def _map_record(rec: dict) -> dict:
    """Translate one Mercell record to a `tenders` row dict."""
    cpv = rec.get("cpvCodes") or []
    if not isinstance(cpv, list):
        cpv = [str(cpv)]
    return {
        "source_system": "mercell",
        "source_id": str(rec.get("id", "")),
        "tender_url": f"https://app.mercell.com/sv-SE/m/tender/{rec.get('id','')}",
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
                yield rec
            time.sleep(delay_s)


def run(db_path: str) -> int:
    """Walk Mercell, upsert everything, log the run. Returns rows written."""
    init_db(db_path)
    conn = connect(db_path)
    written = 0
    try:
        for rec in _walk_pages():
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
    run(os.environ.get("DB_PATH", "/data/agentanbud.db"))
