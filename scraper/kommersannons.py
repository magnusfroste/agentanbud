"""
Kommersannons (Antirio eLite) — a registered annonsdatabas used mostly by
municipalities and municipal companies, i.e. the below-threshold procurements
TED never sees and small companies can actually win.

Not mirrored by Mercell, whatever the README once said: of the 14 open notices
on Kommersannons' front page on 2026-10-06, 10 were in no other source we have.

Source: https://www.kommersannons.se/elite/notice/noticelist.aspx
robots.txt: `allow: /` (checked 2026-10-06). Tendsign and e-Avrop disallow
everything and are not scraped; this one invites it.

How the site works — an ASP.NET WebForms app with no API:
- The list page holds 20 notices. "Nästa" is a form POST that sends back the
  page's own hidden fields (__VIEWSTATE and friends), so paging means
  carrying them from one response into the next request. ~20 pages, ~2 MB each.
- The list row has the title, the kind of notice, deadline date, publish date
  and region, but not the buyer or CPV — those are on NoticeOverview.aspx,
  which also carries the exact deadline in a script call:
  setDeadline('2026-11-16T23:59:50', ...) in Swedish local time.

Only notices you can still act on are stored: calls for tender, qualification
notices and RFIs. Efterannonser (award notices) are skipped — winners sit behind
a login, and an award with no winner is a closed tender listed twice.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import httpx

from app.db import connect, init_db, log_sync, upsert_tender

LOG = logging.getLogger(__name__)

BASE = "https://www.kommersannons.se/elite/notice/"
LIST_URL = BASE + "noticelist.aspx"
DETAIL_URL = BASE + "NoticeOverview.aspx?ProcurementId={pid}"
DEFAULT_USER_AGENT = "agentanbud/0.3 (+https://www.agentanbud.se)"

# The kinds worth a supplier's attention. Award notices are left out on purpose
# (see module docstring).
KEEP_KINDS = {"Meddelande om upphandling", "Kvalificeringsannons", "Rfi"}

MAX_PAGES = 60          # ~20 today; a ceiling so a paging bug can't loop forever
LIST_PAUSE_S = 1.0      # polite: one list page per second
DETAIL_PAUSE_S = 0.5


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", DEFAULT_USER_AGENT)


def _text(fragment: str) -> str:
    """HTML fragment → single-spaced plain text."""
    return html.unescape(re.sub(r"<[^>]+>|\s+", " ", fragment)).strip()


# --- Time -------------------------------------------------------------------

def _last_sunday(year: int, month: int) -> int:
    """Day of month of the last Sunday (month < 12)."""
    last = datetime(year, month + 1, 1) - timedelta(days=1)
    return last.day - (last.weekday() + 1) % 7


def stockholm_to_utc(local: datetime) -> datetime:
    """Swedish wall-clock time → UTC.

    The EU rule, written out rather than relying on the zoneinfo database,
    which the slim base image does not guarantee: summer time runs from 01:00
    UTC on the last Sunday of March to 01:00 UTC on the last Sunday of October.
    """
    y = local.year
    start = datetime(y, 3, _last_sunday(y, 3), 1)    # UTC
    end = datetime(y, 10, _last_sunday(y, 10), 1)    # UTC
    as_summer = local - timedelta(hours=2)
    utc = as_summer if start <= as_summer < end else local - timedelta(hours=1)
    return utc.replace(tzinfo=timezone.utc)


def _iso_utc(local: datetime) -> str:
    return stockholm_to_utc(local).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Parsing ----------------------------------------------------------------

def hidden_fields(page: str) -> dict:
    """The ASP.NET state a postback has to send back."""
    return {name: html.unescape(value) for name, value in re.findall(
        r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"', page)}


def page_position(page: str) -> tuple[int, int]:
    """'1 / 20' under the list → (1, 20)."""
    m = re.search(r'fw-bold text-center">\s*(\d+)\s*/\s*(\d+)', page)
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


def parse_list(page: str) -> list[dict]:
    """One dict per notice row on a list page."""
    rows = []
    for block in re.split(r'<div class="row mt-4">', page)[1:]:
        m = re.search(
            r'NoticeOverview\.aspx\?ProcurementId=(\d+)"><span[^>]*>(.*?)</span></a>'
            r'\s*-\s*(.*?)<small class="text-muted">(.*?)</small>', block, re.S)
        if not m:
            continue
        info = re.search(r'</p>\s*<small>\s*(.*?)</small>', block, re.S)
        info_text = _text(info.group(1)) if info else ""
        deadline = re.search(r"Sista anbudsdag(?:en)? är (\d{4}-\d{2}-\d{2})", info_text)
        shown = re.search(r"visas mellan (\d{4}-\d{2}-\d{2})", info_text)
        place = re.search(r"Utförandeort (.+?)\.?$", info_text)
        rows.append({
            "pid": m.group(1),
            "ref": _text(m.group(2)),
            "title": _text(m.group(3)),
            "kind": _text(m.group(4)),
            "deadline_date": deadline.group(1) if deadline else None,
            "published": shown.group(1) if shown else None,
            "region": place.group(1).strip() if place else None,
        })
    return rows


def parse_detail(page: str) -> dict:
    """Buyer, procedure, description, CPV and exact deadline from NoticeOverview."""
    out: dict = {}
    # The buyer's label differs by notice type ("Ansvarig upphandlande
    # organisation för den här upphandlingen är", "Ansvarig upphandlande
    # organisation", "Ansvarig organisation" on RFIs); the link to the buyer's
    # profile carries the same control id on all of them.
    m = (re.search(r'hlProcuringEntity"[^>]*>(.*?)</a>', page, re.S)
         or re.search(r"Ansvarig (?:upphandlande )?organisation[^<]*</[^>]+>\s*(?:<[^>]+>\s*)*([^<]+)<",
                      page, re.S))
    if m and _text(m.group(1)):
        out["authority"] = _text(m.group(1))
    m = re.search(r"Upphandlingen genomförs med förfarande\s*</[^>]+>\s*(.*?)</", page, re.S)
    if m:
        out["procedure"] = _text(m.group(1))
    m = re.search(r"annonserad publikt\.\s*</p>\s*<p>(.*?)</p>", page, re.S)
    if m and _text(m.group(1)):
        out["description"] = _text(m.group(1))
    m = re.search(r"setDeadline\('(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'", page)
    if m:
        out["deadline"] = _iso_utc(datetime.fromisoformat(m.group(1)))
    # "CPV 45210000-2" → "45210000": the check digit is dropped, as in every
    # other source, so CPV filters match across them.
    out["cpv_codes"] = list(dict.fromkeys(re.findall(r"CPV\s*</[^>]+>\s*<[^>]+>\s*(\d{8})-\d", page)
                                          or re.findall(r"\b(\d{8})-\d\b", page)))
    return out


def map_record(row: dict, detail: dict) -> dict:
    """List row + detail page → a tenders row."""
    deadline = detail.get("deadline")
    if not deadline and row.get("deadline_date"):
        # No exact time on the page: the convention is end of day, local time.
        deadline = _iso_utc(datetime.fromisoformat(row["deadline_date"] + "T23:59:00"))
    return {
        "source_system": "kommersannons",
        "source_id": row["pid"],
        "tender_url": DETAIL_URL.format(pid=row["pid"]),
        "title": row["title"],
        "authority": detail.get("authority") or "",
        "cpv_codes": detail.get("cpv_codes") or [],
        "deadline": deadline,
        "published_at": row.get("published"),
        "description": detail.get("description") or "",
        "value": None,  # Kommersannons does not publish an estimated value
        "procedure": detail.get("procedure"),
        "contract_type": None,
        "document_type": row["kind"],
        "region": row.get("region"),
        "raw_json": json.dumps({"list": row, "detail": detail, "ref": row.get("ref")},
                               ensure_ascii=False),
    }


# --- Duplicates -------------------------------------------------------------

def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^a-zåäö0-9]", "", (s or "").lower())


def known_elsewhere(conn) -> set[tuple[str, str]]:
    """(title, buyer prefix) of every notice another source already holds.

    Some Kommersannons notices are republished on Mercell or TED. Storing them
    again would show the same procurement twice in every search. Buyer names
    are spelled differently between platforms ("KRISTIANSTADS KOMMUN,
    Kristianstad"), so only a normalised prefix is compared.
    """
    rows = conn.execute(
        "SELECT title, authority FROM tenders WHERE source_system != 'kommersannons'").fetchall()
    return {(_norm(r[0]), _norm(r[1])[:10]) for r in rows}


# --- Crawl ------------------------------------------------------------------

def walk_list(client: httpx.Client) -> Iterator[dict]:
    """Yield every row on every list page, following the postback pager."""
    page = client.get(LIST_URL).text
    for _ in range(MAX_PAGES):
        yield from parse_list(page)
        current, total = page_position(page)
        if current >= total:
            return
        form = hidden_fields(page)
        form["ctl00$ctl00$content$Content$btnNext"] = "Nästa"
        time.sleep(LIST_PAUSE_S)
        page = client.post(LIST_URL, data=form).text
        if page_position(page)[0] <= current:
            LOG.warning("kommersannons: pager did not advance past page %d", current)
            return


def run(db_path: str) -> int:
    """Crawl Kommersannons, upsert the actionable notices. Returns rows written."""
    init_db(db_path)
    conn = connect(db_path)
    written = skipped_dup = skipped_kind = 0
    try:
        elsewhere = known_elsewhere(conn)
        stored = {r[0]: r[1] for r in conn.execute(
            "SELECT source_id, raw_json FROM tenders WHERE source_system = 'kommersannons'")}
        with httpx.Client(headers={"User-Agent": _user_agent()}, timeout=60,
                          follow_redirects=True) as client:
            seen = set()
            for row in walk_list(client):
                if row["pid"] in seen:
                    continue
                seen.add(row["pid"])
                if row["kind"] not in KEEP_KINDS:
                    skipped_kind += 1
                    continue
                # Reuse the stored detail when the list row is unchanged: the
                # detail page is the slow, per-notice request.
                detail = None
                if row["pid"] in stored:
                    try:
                        prev = json.loads(stored[row["pid"]])
                        if prev.get("list") == row:
                            detail = prev.get("detail")
                    except (TypeError, ValueError):
                        pass
                if detail is None:
                    time.sleep(DETAIL_PAUSE_S)
                    try:
                        detail = parse_detail(client.get(DETAIL_URL.format(pid=row["pid"])).text)
                    except Exception as exc:
                        LOG.warning("kommersannons detail %s failed: %s", row["pid"], exc)
                        detail = {}
                rec = map_record(row, detail)
                if row["pid"] not in stored and \
                        (_norm(rec["title"]), _norm(rec["authority"])[:10]) in elsewhere:
                    skipped_dup += 1
                    continue
                upsert_tender(conn, rec)
                written += 1
        conn.commit()
        msg = (f"kommersannons.se: {written} sparade, {skipped_dup} fanns redan i annan källa, "
               f"{skipped_kind} efterannonser hoppades över")
        log_sync(conn, source="kommersannons", status="ok", count=written, message=msg)
        LOG.info(msg)
        return written
    except Exception as exc:
        conn.rollback()
        log_sync(conn, source="kommersannons", status="error", count=written, message=str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    run(os.environ.get("DB_PATH", "/data/application.db"))
