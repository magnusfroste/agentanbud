"""
TED EU v3 Search API client — updated for eForms field names.

The API uses POST with a JSON body containing query + fields + pagination.
Fields follow the eForms BT-* standard. We extract Swedish titles,
buyer names, CPV codes, deadlines, values, and procedure types.

Coverage: EU-threshold procurements only (~6500 SWE notices / 90 days).
Historical data available back to 2016 (208k total SWE notices).
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
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_LIMIT = 100
MAX_PAGES = 100

# eForms field names (verified against the API's supported-values list)
TED_FIELDS = [
    "publication-number",
    "publication-date",
    "notice-title",
    "buyer-name",
    "organisation-country-buyer",
    "deadline-receipt-tender-date-lot",
    "classification-cpv",
    "estimated-value-lot",
    "estimated-value-cur-lot",
    "procedure-type",
    "cvd-contract-type-lot",
    "place-of-performance-country-proc",
    "title-proc",
]


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)


def _extract_swedish(field_val) -> str:
    """TED returns multi-language dicts. Extract Swedish or English."""
    if isinstance(field_val, dict):
        return field_val.get("swe") or field_val.get("eng") or next(iter(field_val.values()), "")
    if isinstance(field_val, list):
        return _extract_swedish(field_val[0]) if field_val else ""
    return str(field_val or "")


def _extract_title(rec: dict) -> str:
    """Title can be in notice-title (multilang dict) or title-proc."""
    t = rec.get("notice-title") or rec.get("title-proc")
    return _extract_swedish(t)


def _extract_buyer(rec: dict) -> str:
    """Buyer name is a multilang dict of lists."""
    b = rec.get("buyer-name")
    if isinstance(b, dict):
        val = b.get("swe") or b.get("eng") or next(iter(b.values()), [])
        if isinstance(val, list):
            return val[0] if val else ""
        return str(val)
    return str(b or "")


def _extract_cpv(rec: dict) -> list:
    """CPV codes come as a flat list, possibly duplicated."""
    cpv = rec.get("classification-cpv")
    if isinstance(cpv, list):
        # Dedupe while preserving order
        seen = set()
        unique = []
        for c in cpv:
            if c not in seen:
                seen.add(c)
                unique.append(str(c))
        return unique
    return [str(cpv)] if cpv else []


def _extract_value(rec: dict) -> Optional[float]:
    """Estimated value in SEK (if available)."""
    v = rec.get("estimated-value-lot")
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None



def _extract_deadline(val) -> Optional[str]:
    """Deadline can be a string or a list of strings."""
    if isinstance(val, list):
        return val[0] if val else None
    return val

def _map_record(rec: dict) -> dict:
    """Translate one TED notice to a `tenders` row dict."""
    pub_no = str(rec.get("publication-number", ""))
    title = _extract_title(rec)
    # Clean up TED titles: "Sverige – IT-tjänster – Actual title"
    if " – " in title:
        parts = title.split(" – ", 2)
        if len(parts) >= 3:
            title = parts[-1]  # last part is the actual title

    return {
        "source_system": "ted",
        "source_id": pub_no,
        # /en/notice/-/detail/{nr} is the only form that renders in a
        # browser — both /en/notice/{nr} and /en/notice/{nr}/html bounce
        # users to the TED homepage (the /html body is fine for bots).
        "tender_url": f"https://ted.europa.eu/en/notice/-/detail/{pub_no}",
        "title": title.strip(),
        "authority": _extract_buyer(rec).strip(),
        "cpv_codes": json.dumps(_extract_cpv(rec), ensure_ascii=False),
        "deadline": _extract_deadline(rec.get("deadline-receipt-tender-date-lot")),
        "published_at": rec.get("publication-date"),
        "description": "",  # TED v3 doesn't return full description in search
        "value": _extract_value(rec),
        "procedure": rec.get("procedure-type"),
        "contract_type": _extract_swedish(rec.get("cvd-contract-type-lot")),
        "document_type": _extract_swedish(rec.get("notice-title", {}))[:50] or None,
        "region": "Sverige",  # TED doesn't give län-level, just country
        "raw_json": json.dumps(rec, ensure_ascii=False),
    }


def _fetch_page(query: str, fields: list, limit: int, page: int) -> dict:
    """Fetch one page of TED notices."""
    body = {
        "query": query,
        "fields": fields,
        "limit": limit,
        "page": page,
    }
    ua = _user_agent()
    for attempt in range(3):
        r = httpx.post(API_URL, json=body,
                       headers={"User-Agent": ua, "Accept": "application/json"},
                       timeout=30)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "30"))
            LOG.warning("ted 429, sleeping %ds", retry_after)
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
    """Yield TED notices for Sweden, paginating until done."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y%m%d")
    query = f"buyer-country = SWE AND publication-date >= {cutoff}"

    for page in range(1, max_pages + 1):
        try:
            data = _fetch_page(query, TED_FIELDS, limit, page)
        except Exception as exc:
            LOG.warning("ted page %d fetch failed: %s", page, exc)
            return
        notices = data.get("notices", [])
        if not notices:
            LOG.info("ted page %d empty, stopping", page)
            return
        for rec in notices:
            yield rec
        # Check if we've reached the last page
        total = data.get("totalNoticeCount", 0)
        if page * limit >= total:
            LOG.info("ted reached total %d at page %d", total, page)
            return
        time.sleep(0.3)  # polite


def run(db_path: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> int:
    """Walk TED, upsert everything, log the run. Returns rows written."""
    init_db(db_path)
    conn = connect(db_path)
    written = 0
    try:
        for rec in walk_notices(lookback_days=lookback_days):
            try:
                upsert_tender(conn, _map_record(rec))
                written += 1
            except Exception as exc:
                LOG.warning("ted record %r failed: %s", rec.get("publication-number"), exc)
        conn.commit()
        log_sync(conn, source="ted", status="ok", count=written,
                 message=f"lookback {lookback_days}d")
        LOG.info("ted: wrote/updated %d tenders (lookback %dd)", written, lookback_days)
        return written
    except Exception as exc:
        conn.rollback()
        log_sync(conn, source="ted", status="error", count=written, message=str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run(os.environ.get("DB_PATH", "/data/application.db"))
