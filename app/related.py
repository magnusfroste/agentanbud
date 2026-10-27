"""
What a tender page adds to the notice: CPV codes in words, similar open
tenders, and what we know about the buyer.

A tender page used to show what the source shows — title, dates, description,
bare CPV numbers — so to a reader and to a search engine it was a copy of the
notice. The MCP tools already answered the questions a supplier asks next
(similar_tenders, get_authority, get_winner_history); this brings the same
answers to the page, from the same data.

Performance: everything here is answered from an in-memory snapshot of the
table, built in one pass and rebuilt in the background when the data changes
(at most once a minute) or every hour. The first version queried the
database on each page view — similar tenders by LIKE over every row's CPV
list, the buyer by scanning its rows — and on 24,000 tenders that took a page
from 6 ms to 41 ms and sixteen concurrent page views from 179 pages a second
to 20. A crawler reads tender pages sixteen at a time. From the snapshot a
page costs about what it did before.

CPV labels are the EU's own Swedish names (CPV 2008, 9,454 codes), fetched
from the Publications Office (data.europa.eu/cpv) into app/vocab/cpv_sv.json (not
"data/", which git and the Docker build ignore: that is where the database
lives).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app import seo

LOG = logging.getLogger(__name__)

CPV_FILE = Path(__file__).parent / "vocab" / "cpv_sv.json"


@lru_cache(maxsize=1)
def _labels() -> dict:
    try:
        return json.loads(CPV_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cpv_label(code: str) -> Optional[str]:
    """The Swedish name of a CPV code, or of its nearest parent.

    Sources drop the check digit and sometimes use codes finer than the
    vocabulary; zeroing digits from the right walks up the tree
    (45331100 → 45331000 → 45330000 …) until a named code is found.
    """
    code = "".join(ch for ch in str(code or "") if ch.isdigit())[:8].ljust(8, "0")
    if len(code) != 8 or code == "00000000":
        return None
    labels = _labels()
    for keep in range(8, 1, -1):
        candidate = code[:keep].ljust(8, "0")
        if candidate in labels:
            return labels[candidate]
    return None


def cpv_list(codes) -> list[dict]:
    return [{"code": str(c), "label": cpv_label(c)} for c in (codes or [])]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Snapshot:
    """Open tenders by CPV group and by buyer, and a profile per buyer.

    Immutable once built; a rebuild replaces it whole. Deadlines are kept, not
    an "open" flag, so a tender that closes during the day drops out at once.
    """

    def __init__(self, conn, version):
        self.version, self.built = version, time.monotonic()
        now = _now_iso()
        self.items: dict = {}          # id -> (title, authority, deadline, region, cpv set, date text)
        self.by_group: dict = {}       # first four CPV digits -> [ids]
        self.by_authority: dict = {}   # authority exactly as written -> [ids]
        self.buyers: dict = {}         # lower-cased name without town -> profile parts
        for r in conn.execute("SELECT id, source_system, title, authority, deadline, region, cpv_codes FROM tenders"):
            try:
                codes = {str(c) for c in json.loads(r["cpv_codes"] or "[]") if c}
            except (TypeError, ValueError):
                codes = set()
            key = seo._buyer(r["authority"]).lower()
            if key:
                b = self.buyers.setdefault(key, {"name": seo._buyer(r["authority"]), "deadlines": [],
                                                 "notices": 0, "divisions": Counter(),
                                                 "wins": Counter(), "contracts": 0})
                b["divisions"].update({c[:2] for c in codes})
                if r["source_system"] != "ted_awards":
                    b["notices"] += 1
                    if r["deadline"]:
                        b["deadlines"].append(r["deadline"])
            if r["source_system"] == "ted_awards" or not r["deadline"] or r["deadline"] <= now:
                continue
            self.items[r["id"]] = (r["title"], r["authority"], r["deadline"], r["region"], codes,
                                   seo._swedish_date(r["deadline"]))
            for g in {c[:4] for c in codes if len(c) >= 4}:
                self.by_group.setdefault(g, []).append(r["id"])
            if r["authority"]:
                self.by_authority.setdefault(r["authority"].strip(), []).append(r["id"])
        # Who each buyer awarded contracts to, counted as get_winner_history
        # counts: TED republications collapsed, spellings of one supplier merged.
        from mcp_shared import AWARD_DEDUP_KEY, canonical_supplier
        spellings: dict = {}
        for authority, winners_json in conn.execute(
                "SELECT authority, winner_name FROM tenders WHERE source_system = 'ted_awards' "
                f"AND winner_name IS NOT NULL AND winner_name != '' {AWARD_DEDUP_KEY}"):
            b = self.buyers.get(seo._buyer(authority).lower())
            try:
                winners = json.loads(winners_json)
            except (TypeError, ValueError):
                continue
            if b is None or not winners:
                continue
            b["contracts"] += 1
            for w in winners:
                k = canonical_supplier(w)
                if k:
                    b["wins"][k] += 1
                    spellings.setdefault(k, Counter())[w] += 1
        self.label = {k: c.most_common(1)[0][0] for k, c in spellings.items()}


_SNAPSHOTS: dict = {}
_LOCK = threading.Lock()
_BUILDING: set = set()
FRESH_S = 3600       # rebuilt in the background every hour regardless (deadlines
                     # are checked at query time, so this is only a backstop)
MIN_REBUILD_S = 60   # and when the data changed, but not more than once a minute


def _version(conn) -> tuple:
    return tuple(conn.execute("SELECT COUNT(*), MAX(id) FROM tenders").fetchone())


def _path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def _rebuild(path: str) -> None:
    from app.db import connect
    try:
        conn = connect(path)
        try:
            snap = _Snapshot(conn, _version(conn))
        finally:
            conn.close()
        with _LOCK:
            _SNAPSHOTS[path] = snap
    except Exception:
        LOG.exception("related snapshot rebuild failed")
    finally:
        with _LOCK:
            _BUILDING.discard(path)


def _snapshot(conn) -> _Snapshot:
    """The current snapshot; the first one is built in the request, later ones
    in the background while the old one keeps answering."""
    path = _path(conn)
    with _LOCK:
        snap = _SNAPSHOTS.get(path)
    if snap is None:
        with _LOCK:
            snap = _SNAPSHOTS.get(path)
            if snap is None:
                snap = _SNAPSHOTS[path] = _Snapshot(conn, _version(conn))
        return snap
    age = time.monotonic() - snap.built
    if age >= FRESH_S or (age >= MIN_REBUILD_S and _version(conn) != snap.version):
        with _LOCK:
            if path not in _BUILDING:
                _BUILDING.add(path)
                threading.Thread(target=_rebuild, args=(path,), daemon=True, name="related-rebuild").start()
    return snap


def warm(path: str) -> threading.Thread:
    """Build the first snapshot in the background, so the first tender page
    after a start does not pay for it (about 0.3 s at 24,000 tenders)."""
    with _LOCK:
        _BUILDING.add(path)
    t = threading.Thread(target=_rebuild, args=(path,), daemon=True, name="related-warm")
    t.start()
    return t


def reset() -> None:
    """Forget every snapshot (tests change the data and look again at once)."""
    with _LOCK:
        _SNAPSHOTS.clear()


def similar_open(conn, t: dict, limit: int = 5) -> list[dict]:
    """Open tenders sharing a CPV group (first four digits) or the buyer.

    Scored as the MCP tool similar_tenders does: two points per shared CPV
    code, three for the same buyer; soonest deadline breaks ties.
    """
    snap = _snapshot(conn)
    codes = {str(c) for c in (t.get("cpv_codes_list") or [])}
    buyer = (t.get("authority") or "").strip()
    candidates = set(snap.by_authority.get(buyer, ())) if buyer else set()
    for g in {c[:4] for c in codes if len(c) >= 4}:
        candidates.update(snap.by_group.get(g, ()))
    candidates.discard(t.get("id"))
    now = _now_iso()
    scored = []
    for i in candidates:
        title, authority, deadline, region, theirs, _ = snap.items[i]
        if deadline <= now:
            continue
        score = 2 * len(codes & theirs) + (3 if buyer and authority == buyer else 0)
        if score:
            scored.append((-score, deadline, i))
    scored.sort()
    out = []
    for _, deadline, i in scored[:limit]:
        title, authority, _, region, _, when = snap.items[i]
        out.append({"id": i, "title": title, "authority": authority, "region": region,
                    "deadline_text": when})
    return out


def buyer_profile(conn, authority: Optional[str]) -> Optional[dict]:
    """Open notices, usual industries and usual winners for this buyer.

    The buyer is its name as the sources write it, with or without the
    ", TOWN" Mercell and TED append and in any case — "Region Halland",
    "REGION HALLAND" and "Region Halland, HALMSTAD" are one buyer — never a
    substring, which for a name like "Region" would mean every region.
    """
    name = seo._buyer(authority)
    if len(name) < 4:
        return None
    b = _snapshot(conn).buyers.get(name.lower())
    if b is None:
        return None
    snap = _snapshot(conn)
    now = _now_iso()
    return {
        "name": name,
        "open": sum(1 for d in b["deadlines"] if d > now),
        "notices": b["notices"],
        "usual": [{"code": d, "label": cpv_label(d + "000000") or f"CPV {d}", "n": n}
                  for d, n in b["divisions"].most_common(3)],
        "winners": [{"name": snap.label.get(k, k), "n": n} for k, n in b["wins"].most_common(3)],
        "contracts": b["contracts"],
    }
