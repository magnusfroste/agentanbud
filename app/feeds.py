"""
Feeds without an account: RSS for new tenders, iCal for their deadlines.

Every official alert service we looked at (Hilma, Contracts Finder, e-Avrop,
HigherGov, November 2026) needs an account and sends email. These need
neither: any search, industry, county or buyer page has a feed.xml (RSS
2.0, newest first) and a kalender.ics (each open tender's last day for
bids), which a reader, Slack or Teams, a calendar or an agent polls. Nothing
is stored about who subscribes — the same promise as the rest of the site.

The selection is the page's own: open tenders (a deadline ahead, or none),
award notices excluded; a search uses the search's own matching
(app/search.build_match with the LIKE floor). The calendar holds only
tenders with a deadline, since an event needs a time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Iterable, Optional
from xml.sax.saxutils import escape

from app import seo

LIMIT = 50
CACHE_HEADERS = {"Cache-Control": "public, max-age=900"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    v = str(value).strip().replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(v if "T" in v or " " in v else v + "T00:00:00+00:00")
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


_COLUMNS = "tenders.id, title, authority, region, deadline, published_at, description"
_OPEN = "source_system != 'ted_awards' AND (deadline IS NULL OR deadline > ?)"


def search_rows(conn, q: str = "", cpv: str = "", authority: str = "", region: str = "",
                limit: int = LIMIT) -> list:
    """Open tenders for a search, newest first."""
    from app.search import build_match, like_floor_sql, match_subquery
    where, args, join = [_OPEN], [_iso(_now())], ""
    if authority:
        where.append("authority LIKE ?")
        args.append(f"%{authority}%")
    if region:
        where.append("region LIKE ?")
        args.append(f"%{region}%")
    if cpv:
        where.append("cpv_codes LIKE ?")
        args.append('%"' + "".join(ch for ch in cpv if ch.isdigit()) + "%")
    match = build_match(conn, q) if q else None
    if match:
        join = f"LEFT JOIN ({match_subquery()}) m ON m.id = tenders.id"
        where.append(like_floor_sql())
        args = [match] + args + [f"%{q}%", f"%{q}%"]
    elif q:
        where.append("(title LIKE ? OR description LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    return conn.execute(
        f"SELECT {_COLUMNS} FROM tenders {join} WHERE {' AND '.join(where)} "
        f"ORDER BY published_at DESC, tenders.id DESC LIMIT ?", args + [limit]).fetchall()


def hub_rows(conn, hub, limit: int = LIMIT) -> list:
    from app import hubs
    where, args = hubs._where(hub, _iso(_now()))
    return conn.execute(f"SELECT {_COLUMNS} FROM tenders WHERE {where} "
                        f"ORDER BY published_at DESC, id DESC LIMIT ?", args + [limit]).fetchall()


def id_rows(conn, ids: Iterable[int], limit: int = LIMIT) -> list:
    ids = list(ids)[:1000]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT {_COLUMNS} FROM tenders WHERE id IN ({marks}) AND {_OPEN} "
                        f"ORDER BY published_at DESC, id DESC LIMIT ?", ids + [_iso(_now()), limit]).fetchall()


def _summary(r) -> str:
    parts = [seo._buyer(r["authority"]) or (r["authority"] or "")]
    if r["deadline"]:
        parts.append(f"Sista anbudsdag {seo._swedish_date(r['deadline'])}")
    if r["region"]:
        parts.append(r["region"])
    return " · ".join(p for p in parts if p)


def rss(rows: list, title: str, page_path: str, description: str) -> str:
    """RSS 2.0, newest first."""
    link = seo.SITE_URL + page_path
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
           f"<title>{escape(title)}</title>", f"<link>{escape(link)}</link>",
           f"<description>{escape(description)}</description>", "<language>sv</language>",
           f"<lastBuildDate>{format_datetime(_now())}</lastBuildDate>", "<ttl>60</ttl>"]
    for r in rows:
        url = f"{seo.SITE_URL}/tenders/{r['id']}"
        pub = _parse(r["published_at"])
        text = _summary(r)
        if r["description"]:
            text += "\n\n" + str(r["description"])[:600]
        out.append("<item>")
        out.append(f"<title>{escape(r['title'] or '(utan titel)')}</title>")
        out.append(f"<link>{url}</link>")
        out.append(f'<guid isPermaLink="true">{url}</guid>')
        if pub:
            out.append(f"<pubDate>{format_datetime(pub)}</pubDate>")
        out.append(f"<description>{escape(text)}</description>")
        out.append("</item>")
    out.append("</channel></rss>")
    return "\n".join(out)


def _ics_text(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545: lines over 75 octets continue on the next, after a space."""
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75 if not out else 74
        while cut and (b[cut] & 0xC0) == 0x80:     # never split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b[cut:]
    out.append(b.decode("utf-8"))
    return [out[0]] + [" " + x for x in out[1:]]


def ics(rows: list, title: str) -> str:
    """One event per tender, at its last day for bids, with a reminder three days before."""
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Agentanbud//Sista anbudsdag//SV",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_text(title)}",
             "X-PUBLISHED-TTL:PT1H"]
    for r in rows:
        end = _parse(r["deadline"])
        if not end:
            continue
        url = f"{seo.SITE_URL}/tenders/{r['id']}"
        lines += ["BEGIN:VEVENT", f"UID:tender-{r['id']}@agentanbud.se", f"DTSTAMP:{stamp}",
                  f"DTSTART:{(end - timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}",
                  f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
                  f"SUMMARY:{_ics_text('Sista anbudsdag: ' + (r['title'] or '(utan titel)'))}",
                  f"DESCRIPTION:{_ics_text(_summary(r) + chr(10) + url)}", f"URL:{url}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-P3D",
                  f"DESCRIPTION:{_ics_text('Tre dagar kvar: ' + (r['title'] or ''))}", "END:VALARM",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(x for line in lines for x in _fold(line)) + "\r\n"
