"""
Q&A from Upphandlingsmyndigheten Frågeportalen (Question Portal).

Public autocomplete API at /api/sv/questionportal/autocomplete.
~1,700+ answered questions on LOU, LOV, upphandling.

The endpoint is an *autocomplete* — returns at most 5 results per query.
To build a corpus we run a curated list of common Swedish procurement
terms and dedupe by documentId. Typical coverage: ~150-300 unique
questions per sync.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterator

import httpx

from app.db import connect, init_db, log_sync, upsert_knowledge

LOG = logging.getLogger(__name__)

API_URL = "https://www.upphandlingsmyndigheten.se/api/sv/questionportal/autocomplete"
DEFAULT_USER_AGENT = "agentanbud/0.1"

# Curated query list — common procurement terms in Swedish.
# Order matters roughly by expected volume. Each yields 5 unique docs.
QUERIES = [
    # Single letters (high recall for partial matches)
    "a", "e", "i", "o", "u", "å", "ä", "ö",
    # Common terms
    "upphandling", "upphandla", "avtal", "kontrakt", "anbud",
    "LOU", "LOV", "LUF", "LUK",  # the laws
    "ramavtal", "direktupphandling", "auktorisation",
    "tröskelvärde", "annonsering", "upphandlingsförfarande",
    "anbudsöppning", "utvärdering", "tilldelning", "avbrytande",
    "överprövning", "skadestånd", "upphandlingsskadeavgift",
    # Sectors
    "bygg", "it", "vård", "omsorg", "skola", "transport", "livsmedel",
    "städ", "kontor", "fordon", "energi", "avfall",
    # Special terms
    "dialog", "referens", "option", "förlängning", "ändring",
    "underleverantör", "konsult", "varor", "tjänster",
    # People
    "kommun", "region", "myndighet", "upphandlare",
]


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)


def _strip_html(text: str) -> str:
    """Strip <mark> highlight tags from search excerpts."""
    if not text:
        return ""
    return text.replace("<mark>", "").replace("</mark>", "")


def _map_record(rec: dict) -> dict:
    """Map one question result to a knowledge row."""
    categories = rec.get("categories", []) or []
    return {
        "source_system": "questions",
        "source_id": rec.get("documentId", ""),
        "url": "https://www.upphandlingsmyndigheten.se" + rec.get("url", ""),
        "title": rec.get("heading", "").strip(),
        "category": categories[0] if categories else None,
        "subcategory": categories[1] if len(categories) > 1 else None,
        "tags": json.dumps(categories, ensure_ascii=False),
        "excerpt": _strip_html(rec.get("excerpt", "")),
        "body": _strip_html(rec.get("excerpt", "")),  # no full body in autocomplete
        "raw_json": json.dumps(rec, ensure_ascii=False),
    }


def walk_records() -> Iterator[dict]:
    """Yield unique questions across all curated queries.

    Dedupe by documentId so we only upsert each question once even
    though it shows up for multiple query terms.
    """
    seen: set[str] = set()
    for query in QUERIES:
        try:
            r = httpx.get(API_URL, params={"query": query, "fetch": 5},
                          timeout=30,
                          headers={"User-Agent": _user_agent(), "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            LOG.warning("questions query=%r failed: %s", query, exc)
            time.sleep(1)
            continue
        for rec in data.get("results", []):
            did = rec.get("documentId")
            if did and did not in seen:
                seen.add(did)
                yield rec
        time.sleep(0.2)


def run(db_path: str) -> int:
    """Walk all queries, upsert unique questions, log the run."""
    init_db(db_path)
    conn = connect(db_path)
    written = 0
    try:
        for rec in walk_records():
            try:
                upsert_knowledge(conn, _map_record(rec))
                written += 1
            except Exception as exc:
                LOG.warning("questions record %r failed: %s", rec.get("documentId"), exc)
        conn.commit()
        log_sync(conn, source="questions", status="ok", count=written,
                 message=f"frageportalen.se autocomplete, {len(QUERIES)} queries")
        LOG.info("questions: wrote/updated %d items", written)
        return written
    except Exception as exc:
        conn.rollback()
        log_sync(conn, source="questions", status="error", count=written, message=str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run(os.environ.get("DB_PATH", "/data/application.db"))
