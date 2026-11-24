"""
Browsers that run JavaScript, and programs that only say they are browsers.

A pageview is counted as a person when the user agent does not name a bot
(main._looks_like_bot). In November 2026 that was not enough: a crawler that
sends an ordinary browser user agent from a new address for every page read
about a thousand pages a day, one page per "visitor", and was counted as a
thousand people (on 15 November 3,703 of them).

What such a program does not do is run the page's JavaScript. Every page
(web/templates/base.html) sends one empty POST to /api/seen when it has
loaded; that marks the visitor id (app/visitors.py: a hash of the day's salt,
the address and the user agent) as a browser that runs scripts, for that day.

Once a minute (app/ops.py) reclassify() looks at the human pageviews older
than GRACE_S: a view whose visitor never ran the script that day becomes a
crawler view, counted the way crawler views always are (daily_counters
bot_view and bot_view:<page type>, plus nojs_view so the figure is not
hidden), and its row is removed. Views from before the first mark are left
as they were, so nothing logged before this existed is reclassified.

What this misses, both ways: a person with JavaScript turned off is counted
as a crawler, and a crawler that drives a real browser is counted as a
person. The first is rare; the second costs a crawler far more per page.

Nothing new is stored about anyone: browser_js holds the same daily visitor
id usage_log already holds, and is pruned with it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

LOG = logging.getLogger(__name__)

GRACE_S = 900          # a view's script gets this long to report back
LOOKBACK_DAYS = 2      # only recent views are looked at again
KEEP_TYPES = ("flode",)  # pages without a script: RSS and calendar feeds


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def mark(conn, v: Optional[str], now: Optional[datetime] = None) -> None:
    """This visitor id ran the page script today."""
    if not v:
        return
    now = now or datetime.now(timezone.utc)
    conn.execute("INSERT OR IGNORE INTO browser_js (day, v, first_at) VALUES (?, ?, ?)",
                 (now.strftime("%Y-%m-%d"), v, _ts(now)))
    conn.commit()


def reclassify(conn, now: Optional[datetime] = None) -> int:
    """Turn human pageviews whose visitor never ran the page script into
    crawler views. Returns how many."""
    from app.insights import page_type
    start = conn.execute("SELECT MIN(first_at) FROM browser_js").fetchone()[0]
    if not start:
        return 0                                  # no browser has reported yet
    now = now or datetime.now(timezone.utc)
    since = max(start, _ts(now - timedelta(days=LOOKBACK_DAYS)))
    rows = conn.execute(
        "SELECT id, DATE(created_at) AS day, query FROM usage_log "
        "WHERE action = 'view' AND channel = 'browser' AND created_at >= ? AND created_at < ? "
        "AND json_extract(meta, '$.bot') = 0 AND json_extract(meta, '$.v') IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM browser_js j WHERE j.day = DATE(usage_log.created_at) "
        "AND j.v = json_extract(usage_log.meta, '$.v'))",
        (since, _ts(now - timedelta(seconds=GRACE_S)))).fetchall()
    # A feed is read by a feed reader, which runs no script and is not a
    # crawler pretending: feeds keep their own count.
    rows = [r for r in rows if page_type(r["query"]) not in KEEP_TYPES]
    if not rows:
        return 0
    # Counters and deletes in one transaction: a crash between the two must
    # not leave a view both counted as a crawler and still in the log.
    conn.executemany(
        "INSERT INTO daily_counters (day, kind, n) VALUES (?, ?, 1) "
        "ON CONFLICT(day, kind) DO UPDATE SET n = n + 1",
        [(r["day"], k) for r in rows for k in ("bot_view", f"bot_view:{page_type(r['query'])}", "nojs_view")])
    conn.executemany("DELETE FROM usage_log WHERE id = ?", [(r["id"],) for r in rows])
    conn.commit()
    return len(rows)
