"""
Per-page metadata for search engines and answer engines.

Until October 2026 every one of the ~23,000 tender pages carried the same
meta description and og:title as the front page, so to a crawler they were
near-duplicates and a search snippet said nothing about the tender itself.
These helpers build what is specific to a page — who buys what, by when,
where, from which source — and the schema.org JSON-LD that goes with it.

Everything here is plain text; the templates escape it. JSON-LD goes inside a
<script> element, where HTML escaping does not apply, so jsonld() escapes the
one sequence that could end the element early.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

SITE_URL = "https://www.agentanbud.se"

SOURCE_NAMES = {
    "mercell": "Mercell",
    "ted": "TED",
    "ted_awards": "TED",
    "ted_pin": "TED",
    "lov": "Upphandlingsmyndigheten",
    "kommersannons": "Kommersannons",
}

_MONTHS = ("januari", "februari", "mars", "april", "maj", "juni", "juli",
           "augusti", "september", "oktober", "november", "december")


def _cut(text: str, n: int) -> str:
    """Shorten to n characters at a word boundary, with an ellipsis."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n - 1].rsplit(" ", 1)[0].rstrip(" ,.;:–-")
    return cut + "…"


def _buyer(authority: Optional[str]) -> str:
    """'Fortifikationsverket, ESKILSTUNA' → 'Fortifikationsverket'."""
    return (authority or "").split(",")[0].strip()


def _last_sunday(year: int, month: int) -> int:
    last = datetime(year, month + 1, 1) - timedelta(days=1)
    return last.day - (last.weekday() + 1) % 7


def _swedish_date(iso: Optional[str]) -> Optional[str]:
    """An ISO UTC timestamp → '12 november 2026' in Swedish time.

    A deadline at 23:59 Swedish time is stored as 22:59Z or 21:59Z; one at
    midnight falls on the previous UTC date. The EU summer-time rule is
    written out, as in scraper/kommersannons.py, because the slim base image
    does not guarantee the zoneinfo database.
    """
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(timezone.utc).replace(tzinfo=None)
    start = datetime(t.year, 3, _last_sunday(t.year, 3), 1)
    end = datetime(t.year, 10, _last_sunday(t.year, 10), 1)
    local = t + timedelta(hours=2 if start <= t < end else 1)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"


def _is_past(iso: Optional[str], now: datetime) -> bool:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t < now


def tender_title(t: dict) -> str:
    """'Byte av förångare – Tranåsbostäder', short enough for a result title."""
    title = " ".join((t.get("title") or "Upphandling").split())
    buyer = _buyer(t.get("authority"))
    if buyer and buyer.lower() not in title.lower() and len(title) + len(buyer) <= 70:
        return f"{title} – {buyer}"
    return _cut(title, 70)


def tender_description(t: dict, now: Optional[datetime] = None) -> str:
    """One or two sentences a searcher can act on: who, what, by when, where."""
    now = now or datetime.now(timezone.utc)
    source = t.get("source_system") or ""
    buyer = _buyer(t.get("authority")) or "En offentlig köpare"
    what = _cut(t.get("title") or "en upphandling", 90)
    if source == "ted_awards":
        lead = f"Tilldelning: {buyer} – {what}."
    elif source == "ted_pin":
        lead = f"Förhandsinformation: {buyer} planerar {what}."
    elif source == "lov":
        lead = f"Valfrihetssystem (LOV): {buyer} – {what}."
    else:
        lead = f"{buyer} upphandlar {what}."
    parts = [lead]
    when = _swedish_date(t.get("deadline"))
    if when and source != "ted_awards":
        past = _is_past(t.get("deadline"), now)
        parts.append(f"Sista anbudsdag var {when}." if past else f"Sista anbudsdag {when}.")
    if t.get("region"):
        parts.append(f"{_cut(t['region'], 40)}.")
    name = SOURCE_NAMES.get(source)
    if name:
        parts.append(f"Källa: {name}.")
    return " ".join(parts)


def jsonld(data: dict) -> str:
    """JSON for a <script type="application/ld+json"> element.

    Scraped titles end up in here. HTML escaping does not apply inside a
    script element, so "</" is written as "<\\/", which JSON reads the same.
    """
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def breadcrumbs(*crumbs: tuple[str, str]) -> dict:
    """BreadcrumbList from (name, path) pairs, home first."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": SITE_URL + path}
            for i, (name, path) in enumerate(crumbs, start=1)
        ],
    }


def _iso_date(value: Optional[str]) -> Optional[str]:
    """'2026-10-14 21:44:01' → '2026-10-14T21:44:01Z' (stored times are UTC)."""
    if not value:
        return None
    s = str(value)[:19].replace(" ", "T")
    return s + "Z" if len(s) == 19 else s


def blog_posting(post: dict) -> dict:
    """BlogPosting for one published post."""
    url = f"{SITE_URL}/blogg/{post['slug']}"
    data = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": _cut(post.get("title") or "", 110),
        "description": post.get("summary") or "",
        "url": url,
        "mainEntityOfPage": url,
        "inLanguage": "sv",
        "author": {"@type": "Organization", "name": post.get("author") or "Agentanbud"},
        "publisher": {"@type": "Organization", "name": "Agentanbud", "url": SITE_URL + "/"},
    }
    if post.get("image_url"):
        data["image"] = post["image_url"]
    published = _iso_date(post.get("published_at"))
    if published:
        data["datePublished"] = published
        data["dateModified"] = _iso_date(post.get("updated_at")) or published
    if post.get("tags"):
        data["keywords"] = list(post["tags"])
    return data
