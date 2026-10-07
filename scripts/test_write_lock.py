#!/usr/bin/env python3
"""A scraper mid-run must not lock the site out of its own database.

Every scraper used to commit once, at the end. Python's sqlite3 opens a
transaction at the first INSERT, so the write lock was held for the whole run,
network waits included: Mercell ~80 s, Kommersannons ~4 min. In production
(October 2026) that surfaced as "database is locked" and "pageview logging
failed" while traffic peaked.

Each case runs a scraper's real run() with the network replaced by a generator
that, between two records, writes from a second connection — what a pageview
does. With a short busy timeout that write fails whenever the scraper still
holds the lock.

Run: python3 scripts/test_write_lock.py
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.db import init_db  # noqa: E402
from scraper import criteria, kommersannons, lov, questions, ted, ted_awards, ted_pin  # noqa: E402

TENDER = {
    "source_system": "test", "tender_url": "https://example.invalid", "title": "t",
    "authority": "Testkommun", "cpv_codes": "[]", "deadline": None, "published_at": None,
    "description": "", "value": None, "procedure": None, "contract_type": None,
    "document_type": None, "region": None, "raw_json": "{}",
}
KNOWLEDGE = {
    "source_system": "test", "category": "c", "subcategory": None, "title": "t",
    "url": "https://example.invalid", "body": "", "excerpt": "", "tags": "[]", "raw_json": "{}",
}


def concurrent_write_ok(db: str) -> bool:
    """Write the way the pageview logger does, from its own connection."""
    other = sqlite3.connect(db, timeout=0.3)
    try:
        other.execute("INSERT INTO daily_counters (day, kind, n) VALUES ('2026-01-01', 'probe', 1) "
                      "ON CONFLICT DO NOTHING")
        other.commit()
        return True
    except sqlite3.OperationalError as exc:
        print(f"      concurrent write: {exc}")
        return False
    finally:
        other.close()


def case(name, module, walker, row, run_kwargs=None):
    db = tempfile.mktemp(suffix=".db")
    init_db(db)
    results = []

    def fake_walk(*args, **kwargs):
        for i in range(3):
            yield {"i": i}
            # The scraper has upserted record i and is "waiting on the network".
            results.append(concurrent_write_ok(db))

    counter = iter(range(10**6))
    orig_walk, orig_map = getattr(module, walker), module._map_record
    setattr(module, walker, fake_walk)
    module._map_record = lambda rec: {**row, "source_id": f"{name}-{next(counter)}"}
    try:
        n = module.run(db, **(run_kwargs or {}))
    finally:
        setattr(module, walker, orig_walk)
        module._map_record = orig_map
    ok = all(results) and n == 3
    print(f"  {'PASS' if ok else 'FAIL'}  {name:14} {sum(results)}/{len(results)} writes "
          f"got through mid-run, {n} rows stored")
    return ok


def kommersannons_case():
    """Kommersannons fetches a detail page between rows, inside run()."""
    db = tempfile.mktemp(suffix=".db")
    init_db(db)
    results = []
    rows = [{"pid": str(i), "ref": "", "title": f"t{i}", "kind": "Meddelande om upphandling",
             "deadline_date": "2026-12-01", "published": "2026-10-01", "region": "X"} for i in range(3)]

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            results.append(concurrent_write_ok(db))  # "waiting on the network"
            return type("R", (), {"text": ""})()

    orig_client, orig_walk, orig_pause = kommersannons.httpx.Client, kommersannons.walk_list, \
        kommersannons.DETAIL_PAUSE_S
    kommersannons.httpx.Client = FakeClient
    kommersannons.walk_list = lambda client: iter(rows)
    kommersannons.DETAIL_PAUSE_S = 0
    try:
        n = kommersannons.run(db)
    finally:
        kommersannons.httpx.Client, kommersannons.walk_list = orig_client, orig_walk
        kommersannons.DETAIL_PAUSE_S = orig_pause
    # The first detail fetch happens before anything is written; the rest
    # happen after a row, which is what this test is about.
    ok = all(results) and n == 3
    print(f"  {'PASS' if ok else 'FAIL'}  {'kommersannons':14} {sum(results)}/{len(results)} writes "
          f"got through mid-run, {n} rows stored")
    return ok


print("Sajten kan skriva medan en skrapa kör")
outcomes = [
    case("lov", lov, "walk_notices", TENDER),
    case("ted", ted, "walk_notices", TENDER, {"lookback_days": 1}),
    case("ted_awards", ted_awards, "walk_notices", TENDER, {"lookback_days": 1}),
    case("ted_pin", ted_pin, "walk_notices", TENDER, {"lookback_days": 1}),
    case("criteria", criteria, "walk_records", KNOWLEDGE),
    case("questions", questions, "walk_records", KNOWLEDGE),
    kommersannons_case(),
]
# mercell materialises every page with list() before its first write, so it
# never held the lock across the network; it commits per row like the rest.

if not all(outcomes):
    print(f"\n{outcomes.count(False)} skrapa/skrapor håller skrivlåset under nätverksväntan")
    sys.exit(1)
print("\nAlla fall passerade")
