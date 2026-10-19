"""
How a caller behaves, and what to tell it.

The reads on /mcp are open, and stay open. That rules out the usual answer
to a client that hammers the endpoint — a key, a block, a ban — so this
module takes the other route: an agent reads every word of a response, so
the response is the control surface. A human who refreshes a page a thousand
times will not read a note asking them to stop. A language model will.

What this measured, October 2026: 19 782 MCP sessions of exactly one call,
cycling 139 search terms — 99% of all "agent calls", from one caller opening
a fresh session per request. Not an agent exploring anything; a loop that
asks once per day's worth of data a few thousand times. It was invisible in
the figures (counted as thousands of agents) and it was handled nowhere.

Three things, built on one classification:

  1. A per-day ledger of how each caller behaves — keyed by the visitor id
     from app/visitors.py, which already rides on every tool call, changes
     daily and never stores an address. The loop's thousands of sessions
     collapse to one entry.
  2. Guidance appended to the answer, escalating with the behaviour: you
     have asked this exact question N times since the data last changed;
     every call of yours opened a new session; you are calling faster than
     the data can change. The answer is still given. Only far past any rate
     a thinking agent could reach does a call get paused — as a JSON-RPC
     error with the retry time in it, never a bare 429.
  3. The same classifier run over usage_log, so /analytics can report what
     the loop is (automated) apart from what agents are, instead of adding
     them up and calling the sum adoption.

Everything that decides is a pure function of counts, so the live ledger and
the historical query cannot drift: see classify() and guidance().
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# --- Thresholds -------------------------------------------------------------
# All relative to one fact: the data changes once a day (06:00 UTC sync). An
# agent asking the same question more often than that is spending its own
# budget on identical answers. Numbers are deliberately generous; a real
# agent doing real work should never see tier 2 or above.

REPEAT_NOTE_AT = 5        # same query, since the data last changed → say so
CHURN_SESSIONS_AT = 20    # ≥ this many sessions AND nearly one per call → say so
CHURN_RATIO = 0.8
HOT_PER_HOUR = 300        # calls in the last hour → "faster than the data changes"
PAUSE_PER_HOUR = 1200     # 20/s sustained: not an agent thinking, a loop running
PAUSE_SECONDS = 60
LOOP_SAME_QUERY_AT = 20   # classification: same question this often is automation

CLASSES = ("single", "explorer", "monitor", "loop")


def classify(calls: int, distinct_queries: int, sessions: int, max_same_query: int) -> str:
    """One caller's day, in a word.

    single    one call, then gone — a person trying it, a crawler, unknown yet
    explorer  several calls, several questions — an agent doing work
    monitor   several calls, one question — a watcher; fine if it is not often
    loop      a new session for nearly every call at scale, or the same
              question asked past LOOP_SAME_QUERY_AT since the data changed —
              automation that has not noticed the data moves once a day

    Order matters: loop is checked before explorer, because a loop over a
    fixed term list has many distinct queries and would otherwise read as
    the most diligent explorer on the site. That is exactly how it read.
    """
    if calls <= 1:
        return "single"
    if sessions >= CHURN_SESSIONS_AT and sessions >= CHURN_RATIO * calls:
        return "loop"
    if max_same_query >= LOOP_SAME_QUERY_AT:
        return "loop"
    if distinct_queries >= 2:
        return "explorer"
    return "monitor"


@dataclass
class Entry:
    """One visitor id, one day."""
    calls: int = 0
    sessions: set = field(default_factory=set)
    queries: Counter = field(default_factory=Counter)   # since data_mark
    data_mark: Optional[str] = None                     # updated_at the counts refer to
    recent: deque = field(default_factory=lambda: deque(maxlen=PAUSE_PER_HOUR + 1))
    last_query: Optional[str] = None

    def calls_last_hour(self, now: float) -> int:
        cutoff = now - 3600
        return sum(1 for t in self.recent if t >= cutoff)

    def session_churn(self) -> bool:
        n = len(self.sessions)
        return n >= CHURN_SESSIONS_AT and n >= CHURN_RATIO * self.calls

    def kind(self) -> str:
        mx = max(self.queries.values()) if self.queries else 0
        return classify(self.calls, len(self.queries), len(self.sessions), mx)


class Ledger:
    """In-memory, per day. Resets when the day turns — the visitor ids do too,
    so nothing would match across the boundary anyway. Nothing is persisted:
    the historical view comes from usage_log via classify(), not from here."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: Optional[str] = None
        self._entries: dict[str, Entry] = {}

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day, self._entries = day, {}

    def record(self, visitor: Optional[str], session: Optional[str], query: Optional[str],
               data_mark: Optional[str], now: Optional[float] = None) -> Optional[Entry]:
        """Count one call. Returns the caller's entry, or None without a visitor id
        (stdio, or no CF-Connecting-IP) — then there is nothing to say."""
        if not visitor:
            return None
        now = time.time() if now is None else now
        with self._lock:
            self._roll()
            e = self._entries.setdefault(visitor, Entry())
            if data_mark != e.data_mark:
                # The data moved; repeating a question is reasonable again.
                e.queries.clear()
                e.data_mark = data_mark
            e.calls += 1
            if session and len(e.sessions) < 10_000:
                e.sessions.add(session)
            q = (query or "").strip().lower()
            if q:
                e.queries[q] += 1
                e.last_query = q
            e.recent.append(now)
            return e

    def snapshot(self) -> dict:
        """For /ops: how many callers of each kind today, and the calls they made."""
        with self._lock:
            self._roll()
            out = {k: {"callers": 0, "calls": 0} for k in CLASSES}
            for e in self._entries.values():
                k = e.kind()
                out[k]["callers"] += 1
                out[k]["calls"] += e.calls
            return {"day": self._day, "kinds": out}


LEDGER = Ledger()


# --- What to say -------------------------------------------------------------

def _when(iso: Optional[str]) -> str:
    return (iso or "")[:16].replace("T", " ") + " UTC" if iso else "okänt"


def guidance(e: Optional[Entry], now: Optional[float] = None) -> list[str]:
    """Lines to append to an answer, in escalating order. Empty for a caller
    behaving like an agent. Each line names the behaviour and the fix, because
    "slow down" on its own gives a model nothing to act on."""
    if e is None:
        return []
    now = time.time() if now is None else now
    notes: list[str] = []

    if e.last_query and e.queries.get(e.last_query, 0) >= REPEAT_NOTE_AT:
        n = e.queries[e.last_query]
        notes.append(
            f"_Du har ställt exakt den här frågan {n} gånger sedan datan senast ändrades "
            f"({_when(e.data_mark)}). Svaret var detsamma varje gång och förblir det tills "
            f"nästa hämtning. Spara svaret i stället för att fråga om._")

    if e.session_churn():
        notes.append(
            f"_Varje anrop från dig i dag har öppnat en ny MCP-session ({len(e.sessions)} "
            f"sessioner på {e.calls} anrop). Skicka tillbaka headern `Mcp-Session-Id` du fick "
            f"vid initialize — en session räcker för hela dagen, och är billigare för båda._")

    hot = e.calls_last_hour(now)
    if hot >= HOT_PER_HOUR:
        notes.append(
            f"_{hot} anrop den senaste timmen. Datan ändras en gång per dygn, så allt utöver "
            f"ett anrop per fråga och dygn ger samma svar. För bevakning: fråga en gång efter "
            f"07:00 UTC._")
    return notes


class Paused(Exception):
    """Raised for a call that should wait. Carries what the caller needs to
    comply: how long, and why. Reads are not refused — this is a pause with
    an end time, for a rate no deliberating agent produces."""

    def __init__(self, retry_after: int, calls_last_hour: int, next_update: Optional[str]):
        self.retry_after = retry_after
        self.calls_last_hour = calls_last_hour
        self.next_update = next_update
        super().__init__(self.message())

    def message(self) -> str:
        return (f"Pausad: {self.calls_last_hour} anrop den senaste timmen är mer än datan kan "
                f"motivera — den ändras en gång per dygn (nästa gång {_when(self.next_update)}). "
                f"Försök igen om {self.retry_after} sekunder, och ställ sedan varje fråga högst "
                f"en gång per dygn.")


def check_pause(e: Optional[Entry], next_update: Optional[str], now: Optional[float] = None) -> None:
    """Raise Paused when the caller is past PAUSE_PER_HOUR. Called before the
    tool runs, so a paused call costs nothing and is not counted as served."""
    if e is None:
        return
    now = time.time() if now is None else now
    hot = e.calls_last_hour(now)
    if hot >= PAUSE_PER_HOUR:
        raise Paused(PAUSE_SECONDS, hot, next_update)


# --- The same classification over the log ----------------------------------

def classify_log(conn, where_sql: str = "", args: Optional[list] = None) -> dict:
    """Run classify() over usage_log, per caller per day, and total the calls
    by kind. `where_sql` is appended inside the WHERE (an extra condition with
    leading AND, or empty) so insights can scope it to its period.

    A caller is the visitor id where there is one; older rows have only a
    session hash, and a row with neither is its own anonymous caller per day
    — which can only *under*state a loop, never invent one.
    """
    sql = f"""
        SELECT COUNT(*)                                  AS calls,
               COUNT(DISTINCT LOWER(TRIM(COALESCE(query, '')))) AS distinct_queries,
               COUNT(DISTINCT json_extract(meta, '$._sid'))     AS sessions,
               MAX(cnt)                                 AS max_same_query
        FROM (
            SELECT query, meta,
                   COALESCE(json_extract(meta, '$.v'),
                            json_extract(meta, '$._sid'),
                            'anon:' || DATE(created_at))  AS caller,
                   DATE(created_at)                        AS day,
                   COUNT(*) OVER (PARTITION BY
                        COALESCE(json_extract(meta, '$.v'),
                                 json_extract(meta, '$._sid'),
                                 'anon:' || DATE(created_at)),
                        DATE(created_at),
                        LOWER(TRIM(COALESCE(query, ''))))   AS cnt
            FROM usage_log
            WHERE action LIKE 'tool:%' {where_sql}
        )
        GROUP BY caller, day
    """
    totals = {k: {"callers": 0, "calls": 0} for k in CLASSES}
    for calls, dq, sessions, mx in conn.execute(sql, args or []):
        k = classify(calls or 0, dq or 0, sessions or 0, mx or 0)
        totals[k]["callers"] += 1
        totals[k]["calls"] += calls or 0
    return totals
