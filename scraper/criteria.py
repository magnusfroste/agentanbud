"""
Sustainability criteria from Upphandlingsmyndigheten.

Public API at /api/sv/upphandlingsmyndigheten/criteriaservice/search.
~743 criteria across Swedish industry sectors (IT, transport, food, etc.).
Paginates with fetch+offset (max 100/page).

The criteria are organized in a hierarchy via breadcrumbs:
  Start → Hållbarhetskriterier → <Bransch> → <Sub-bransch> → <Kriterium>

We store: category (bransch from breadcrumb[2]), subcategory (breadcrumb[3]),
all breadcrumb levels as tags.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterator, Optional

import httpx

from app.db import connect, init_db, log_sync, upsert_knowledge

LOG = logging.getLogger(__name__)

API_URL = "https://www.upphandlingsmyndigheten.se/api/sv/upphandlingsmyndigheten/criteriaservice/search"
DEFAULT_USER_AGENT = "agentanbud/0.1"
DEFAULT_FETCH = 100


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)


def _strip_html(text: str) -> str:
    """The excerpt may contain <mark> tags from the search highlight."""
    if not text:
        return ""
    return text.replace("<mark>", "").replace("</mark>", "")


def _map_record(rec: dict) -> dict:
    """Map one criteria result to a knowledge row."""
    crumbs = rec.get("breadcrumbs", {}).get("breadcrumbs", [])
    # breadcrumb[2] = bransch (t.ex. "IT och telekom")
    # breadcrumb[3] = sub-bransch (t.ex. "IT-utrustning")
    category = crumbs[2]["text"] if len(crumbs) > 2 else None
    subcategory = crumbs[3]["text"] if len(crumbs) > 3 else None
    # All breadcrumb levels as searchable tags
    tags = [c.get("text", "") for c in crumbs if c.get("text")]

    return {
        "source_system": "criteria",
        "source_id": rec.get("documentId", ""),
        "url": rec.get("url", ""),
        "title": rec.get("heading", "").strip(),
        "category": category,
        "subcategory": subcategory,
        "tags": json.dumps(tags, ensure_ascii=False),
        "excerpt": _strip_html(rec.get("excerpt", "")),
        "body": _strip_html(rec.get("excerpt", "")),  # no full body in search API
        "raw_json": json.dumps(rec, ensure_ascii=False),
    }


def _fetch_page(fetch: int, offset: int) -> dict:
    """Fetch one page of criteria."""
    params = {"fetch": str(fetch), "offset": str(offset)}
    ua = _user_agent()
    r = httpx.get(API_URL, params=params, timeout=30,
                  headers={"User-Agent": ua, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def walk_records(*, fetch: int = DEFAULT_FETCH) -> Iterator[dict]:
    """Yield all criteria by paginating through the API."""
    offset = 0
    while True:
        try:
            data = _fetch_page(fetch, offset)
        except Exception as exc:
            LOG.warning("criteria page offset=%d fetch failed: %s", offset, exc)
            return
        results = data.get("results", [])
        if not results:
            return
        for rec in results:
            yield rec
        total = data.get("numberOfHits", 0)
        offset += len(results)
        if offset >= total or len(results) < fetch:
            LOG.info("criteria: reached total %d at offset %d", total, offset)
            return
        time.sleep(0.2)


def run(db_path: str) -> int:
    """Walk criteria, upsert everything, log the run. Returns rows written."""
    init_db(db_path)
    conn = connect(db_path)
    written = 0
    try:
        for rec in walk_records():
            try:
                upsert_knowledge(conn, _map_record(rec))
                written += 1
            except Exception as exc:
                LOG.warning("criteria record %r failed: %s", rec.get("documentId"), exc)
        conn.commit()
        log_sync(conn, source="criteria", status="ok", count=written,
                 message="upphandlingsmyndigheten.se criteraservice")
        LOG.info("criteria: wrote/updated %d items", written)
        return written
    except Exception as exc:
        conn.rollback()
        log_sync(conn, source="criteria", status="error", count=written, message=str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run(os.environ.get("DB_PATH", "/data/application.db"))
