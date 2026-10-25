"""
Usage figures served from memory, recomputed in the background, one at a time.

usage_summary scans all of usage_log. At 420,000 rows (a month, October 2026)
that took 10 seconds on a fast machine and 15–40 on the server, and it sits
behind /analytics, /api/analytics, the ops pages and the MCP tool
get_usage_stats. The old cache kept an answer for a minute with no lock: when
it expired, every request that arrived during the next 40 seconds started its
own scan. Each scan made the others slower, every one held a worker thread,
and /api/health waits for a free thread too — a server that stops answering
without exiting, which is what the site did for nine hours on 24 October.

Here:
- one computation at a time, for any period (a lock, not a minute's luck);
- a stale answer is served at once while a background thread refreshes it,
  so a reader never waits on a scan once the period has been computed;
- only the first request for a period after a start waits, and requests
  arriving meanwhile wait for that same computation instead of starting
  their own;
- periods are 1, 7, 30 days or everything (the log keeps 30 days anyway), so
  ?days=2, 3, 4… cannot each start a scan of their own.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

LOG = logging.getLogger(__name__)

PERIODS = (1, 7, 30)
FRESH_S = 600   # served as is for ten minutes, then refreshed in the background


def period(days: Optional[int]) -> Optional[int]:
    """Round a requested window up to one we compute: 1, 7, 30 or None (all)."""
    if days is None:
        return None
    try:
        days = int(days)
    except (TypeError, ValueError):
        return None
    for p in PERIODS:
        if days <= p:
            return p
    return None


class UsageCache:
    def __init__(self, compute: Callable[[Optional[int]], dict], fresh_s: float = FRESH_S,
                 clock: Callable[[], float] = time.monotonic):
        self._compute = compute
        self._fresh_s = fresh_s
        self._clock = clock
        self._items: dict = {}           # period -> (computed_at, summary)
        self._refreshing: set = set()
        self._state = threading.Lock()   # guards _items and _refreshing
        self._work = threading.Lock()    # one computation at a time
        self.computations = 0

    def _run(self, key: Optional[int]) -> dict:
        with self._work:
            summary = self._compute(key)
            self.computations += 1
        with self._state:
            self._items[key] = (self._clock(), summary)
        return summary

    def _refresh(self, key: Optional[int]) -> None:
        try:
            self._run(key)
        except Exception:
            LOG.exception("usage refresh failed for period %r", key)
        finally:
            with self._state:
                self._refreshing.discard(key)

    def get(self, days: Optional[int]) -> dict:
        key = period(days)
        with self._state:
            hit = self._items.get(key)
            if hit and self._clock() - hit[0] >= self._fresh_s and key not in self._refreshing:
                self._refreshing.add(key)
                threading.Thread(target=self._refresh, args=(key,), daemon=True,
                                 name=f"usage-refresh-{key}").start()
        if hit:
            return hit[1]
        # Nothing yet for this period: compute it, once. Requests arriving
        # meanwhile queue on the work lock and then find the answer stored.
        with self._work:
            with self._state:
                hit = self._items.get(key)
            if hit:
                return hit[1]
            summary = self._compute(key)
            self.computations += 1
            with self._state:
                self._items[key] = (self._clock(), summary)
            return summary


    def warm(self, periods=(7, None, 1, 30), delay_s: float = 5.0) -> threading.Thread:
        """Compute these periods in the background, so the first readers
        after a start find an answer instead of queueing for one."""
        def run():
            time.sleep(delay_s)
            for p in periods:
                with self._state:
                    if p in self._items:
                        continue
                try:
                    self._run(p)
                except Exception:
                    LOG.exception("usage warm-up failed for period %r", p)
        t = threading.Thread(target=run, daemon=True, name="usage-warm")
        t.start()
        return t


_CACHES: dict[str, UsageCache] = {}
_CACHES_LOCK = threading.Lock()


def for_db(db_path: str) -> UsageCache:
    """The cache for one database, shared by the web app and the MCP tools."""
    from app.db import connect
    from app.insights import usage_summary

    with _CACHES_LOCK:
        cache = _CACHES.get(db_path)
        if cache is None:
            def compute(days: Optional[int]) -> dict:
                conn = connect(db_path)
                try:
                    return usage_summary(conn, days=days)
                finally:
                    conn.close()
            cache = _CACHES[db_path] = UsageCache(compute)
        return cache
