"""
LOV (Valfrihetssystem) from Upphandlingsmyndigheten — Swedish freedom-of-choice
procurements for welfare services (home care, personal assistance, special housing,
primary care, etc.).

Different from TED and Mercell:
- NO deadline — these are open-ended (you apply any time, get approved as a
  vendor, then serve clients)
- NO value — the buyer pays per-delivered-service, not a fixed contract sum
- Always running — pages are updated when rules/requirements change

Source: https://www.upphandlingsmyndigheten.se/hitta-lov-uppdrag/
API:    https://www.upphandlingsmyndigheten.se/api/sv/hitta-lov/search?query=&fetch=100&offset=0

Discovery: this isakskogstad/Upphandlingsmyndigheten MCP server
documents the same API. The endpoint uses fetch+offset (NOT size+page).
Max fetch per request is 100.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterator, Optional

import httpx

from app.db import connect, init_db, log_sync, upsert_tender

LOG = logging.getLogger(__name__)

API_URL = "https://www.upphandlingsmyndigheten.se/api/sv/hitta-lov/search"
DEFAULT_USER_AGENT = "agentanbud/0.1"
DEFAULT_FETCH = 100  # max the API allows


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)


def _slug_from_url(url: str) -> str:
    """Extract a stable unique ID from the announcement URL.

    Example: /hitta-lov-uppdrag/annonser/salems-kommun/hemtjanst/
    -> 'salems-kommun/hemtjanst'
    """
    parts = url.strip("/").split("/")
    # last two segments: kommun/annons
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return url


def _map_record(rec: dict) -> dict:
    """Translate one LOV result to a tenders row dict.

    LOV has no deadline/value/procedure/CPV — those are LOU/LUF concepts.
    We store what's available and use the excerpt as description.
    """
    url = rec.get("url", "")
    full_url = f"https://www.upphandlingsmyndigheten.se{url}" if url.startswith("/") else url
    source_id = _slug_from_url(url)

    business_areas = rec.get("businessAreas") or []
    description_parts = []
    if rec.get("excerpt"):
        description_parts.append(rec["excerpt"].strip())
    if business_areas:
        description_parts.append("Områden: " + ", ".join(business_areas))

    return {
        "source_system": "lov",
        "source_id": source_id,
        "tender_url": full_url,
        "title": rec.get("heading", "").strip(),
        "authority": rec.get("regionName", "").strip(),
        "cpv_codes": json.dumps([], ensure_ascii=False),  # LOV doesn't expose CPV
        "deadline": None,  # LOV is open-ended, no application deadline
        "published_at": rec.get("updated"),  # best we have — last update date
        "description": "\n\n".join(description_parts),
        "value": None,  # LOV pays per-delivered-service, no fixed contract value
        "procedure": "LOV",  # Lag om valfrihetssystem
        "contract_type": None,
        "document_type": "Valfrihetssystem (LOV)",
        "region": rec.get("regionName", "").strip(),
        "raw_json": json.dumps(rec, ensure_ascii=False),
    }


def _fetch_page(fetch: int, offset: int) -> dict:
    """Fetch one page of LOV announcements."""
    params = {"query": "", "fetch": str(fetch), "offset": str(offset)}
    ua = _user_agent()
    r = httpx.get(API_URL, params=params, timeout=30,
                  headers={"User-Agent": ua, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def walk_notices(
    *,
    fetch: int = DEFAULT_FETCH,
) -> Iterator[dict]:
    """Yield all LOV announcements by paginating through the API."""
    offset = 0
    while True:
        try:
            data = _fetch_page(fetch, offset)
        except Exception as exc:
            LOG.warning("lov page offset=%d fetch failed: %s", offset, exc)
            return
        results = data.get("results", [])
        if not results:
            return
        for rec in results:
            yield rec
        total = data.get("numberOfHits", 0)
        offset += len(results)
        if offset >= total or len(results) < fetch:
            LOG.info("lov: reached total %d at offset %d", total, offset)
            return
        time.sleep(0.2)  # polite


def run(db_path: str) -> int:
    """Walk LOV, upsert everything, log the run. Returns rows written."""
    init_db(db_path)
    conn = connect(db_path)
    written = 0
    try:
        for rec in walk_notices():
            try:
                upsert_tender(conn, _map_record(rec))
                written += 1
            except Exception as exc:
                LOG.warning("lov record %r failed: %s", rec.get("url"), exc)
        conn.commit()
        log_sync(conn, source="lov", status="ok", count=written,
                 message="upphandlingsmyndigheten.se hitta-lov")
        LOG.info("lov: wrote/updated %d announcements", written)
        return written
    except Exception as exc:
        conn.rollback()
        log_sync(conn, source="lov", status="error", count=written, message=str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run(os.environ.get("DB_PATH", "/data/application.db"))
