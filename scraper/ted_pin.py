"""
TED EU Prior Information Notices (PIN) — "what's coming up".

Prior Information Notices are early signals: a buyer announces that they
INTEND to procure something in the near future, before the formal call
for tenders. For small businesses, this is the earliest possible heads-up.

Subtypes 4 (standard PIN) and 5 (sectoral PIN) cover the main cases.
Some PINs include an estimated value or expected publication date for the
future notice — we capture what we can.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import httpx

from app.db import connect, init_db, log_sync, upsert_tender

LOG = logging.getLogger(__name__)

API_URL = "https://api.ted.europa.eu/v3/notices/search"
DEFAULT_USER_AGENT = "agentanbud/0.1"
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_LIMIT = 100
MAX_PAGES = 50  # PINs are lower volume

# eForms subtypes for Prior Information Notices
# 4 = PIN (standard directive)
# 5 = PIN (sectoral directive)
# 25 = PIN used as call for competition (standard)
# 26 = PIN used as call for competition (sectoral)
PIN_SUBTYPES = ["4", "5", "25", "26"]

TED_PIN_FIELDS = [
    "publication-number",
    "publication-date",
    "notice-title",
    "buyer-name",
    "organisation-country-buyer",
    "classification-cpv",
    "estimated-value-lot",
    "estimated-value-cur-lot",
    "procedure-type",
    "cvd-contract-type-lot",
    "place-of-performance-country-proc",
    "title-proc",
    "future-notice",
    "notice-preferred-publication-date",
]


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)


def _extract_swedish(field_val) -> str:
    if isinstance(field_val, dict):
        return _extract_swedish(field_val.get("swe") or field_val.get("eng") or next(iter(field_val.values()), ""))
    if isinstance(field_val, list):
        return _extract_swedish(field_val[0]) if field_val else ""
    return str(field_val or "")


def _extract_list(field_val) -> list:
    if not field_val:
        return []
    if isinstance(field_val, dict):
        for lang in ("swe", "eng"):
            vals = field_val.get(lang)
            if vals:
                return [vals] if isinstance(vals, str) else list(vals)
        return []
    if isinstance(field_val, list):
        seen, result = set(), []
        for item in field_val:
            s = _extract_swedish(item)
            if s and s not in seen:
                seen.add(s)
                result.append(s)
        return result
    return [str(field_val)]


def _extract_value(rec: dict) -> Optional[float]:
    v = rec.get("estimated-value-lot")
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _map_record(rec: dict) -> dict:
    pub_no = str(rec.get("publication-number", ""))
    title = _extract_swedish(rec.get("notice-title") or rec.get("title-proc"))
    if " – " in title:
        parts = title.split(" – ", 2)
        if len(parts) >= 3:
            title = parts[-1]

    desc_parts = ["Förhandsinformation — upphandling planeras."]
    future = rec.get("future-notice")
    if future:
        desc_parts.append(f"Kommande notice: {future}")
    est_date = rec.get("notice-preferred-publication-date")
    if est_date:
        desc_parts.append(f"Forväntad publicering: {est_date}")
    description = " | ".join(desc_parts)

    return {
        "source_system": "ted_pin",
        "source_id": pub_no,
        "tender_url": f"https://ted.europa.eu/en/notice/{pub_no}",
        "title": title.strip(),
        "authority": _extract_swedish(rec.get("buyer-name")).strip(),
        "cpv_codes": json.dumps(_extract_list(rec.get("classification-cpv")), ensure_ascii=False),
        "deadline": None,  # PINs don't have deadlines yet
        "published_at": rec.get("publication-date"),
        "description": description,
        "value": _extract_value(rec),
        "procedure": rec.get("procedure-type"),
        "contract_type": _extract_swedish(rec.get("cvd-contract-type-lot")),
        "document_type": "Prior Information Notice",
        "region": "Sverige",
        "raw_json": json.dumps(rec, ensure_ascii=False),
    }


def _build_subtype_query() -> str:
    parts = [f'notice-subtype = "{s}"' for s in PIN_SUBTYPES]
    return "(" + " OR ".join(parts) + ")"


def _fetch_page(query: str, fields: list, limit: int, page: int) -> dict:
    body = {"query": query, "fields": fields, "limit": limit, "page": page}
    ua = _user_agent()
    for attempt in range(3):
        r = httpx.post(API_URL, json=body,
                       headers={"User-Agent": ua, "Accept": "application/json"},
                       timeout=30)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "30"))
            LOG.warning("ted_pin 429, sleeping %ds", retry_after)
            time.sleep(retry_after)
            continue
        r.raise_for_status()
        return r.json()
    raise Exception("TED rate-limited after 3 retries")


def walk_notices(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = MAX_PAGES,
) -> Iterator[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y%m%d")
    query = f"buyer-country = SWE AND publication-date >= {cutoff} AND {_build_subtype_query()}"

    for page in range(1, max_pages + 1):
        try:
            data = _fetch_page(query, TED_PIN_FIELDS, limit, page)
        except Exception as exc:
            LOG.warning("ted_pin page %d fetch failed: %s", page, exc)
            return
        notices = data.get("notices", [])
        if not notices:
            LOG.info("ted_pin page %d empty, stopping", page)
            return
        for rec in notices:
            yield rec
        total = data.get("totalNoticeCount", 0)
        if page * limit >= total:
            LOG.info("ted_pin reached total %d at page %d", total, page)
            return
        time.sleep(0.3)


def run(db_path: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> int:
    init_db(db_path)
    conn = connect(db_path)
    written = 0
    try:
        for rec in walk_notices(lookback_days=lookback_days):
            try:
                upsert_tender(conn, _map_record(rec))
                written += 1
            except Exception as exc:
                LOG.warning("ted_pin record %r failed: %s", rec.get("publication-number"), exc)
        conn.commit()
        log_sync(conn, source="ted_pin", status="ok", count=written,
                 message=f"lookback {lookback_days}d, subtypes {PIN_SUBTYPES}")
        LOG.info("ted_pin: wrote/updated %d PIN notices (lookback %dd)", written, lookback_days)
        return written
    except Exception as exc:
        conn.rollback()
        log_sync(conn, source="ted_pin", status="error", count=written, message=str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run(os.environ.get("DB_PATH", "/data/agentanbud.db"))
