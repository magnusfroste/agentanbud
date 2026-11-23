#!/usr/bin/env python3
"""The Mercell walk says how far back it reached, and warns when that is
less than a nightly run needs. The public search stops at 10,000 records
across all countries; in November 2026 that was eleven days. Offline.

Run: python3 scripts/test_mercell_reach.py
"""
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper.mercell import MIN_REACH_HOURS, reach_note  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


now = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc).timestamp()
n = reach_note("2026-11-12T10:33:35.013Z", now)
check("eleven days back: said, no warning", n.startswith("bakåt till 2026-11-12 (10.8 dygn)") and "VARNING" not in n, n)
n = reach_note("2026-11-22T20:00:00Z", now)
check(f"under {MIN_REACH_HOURS} h back: a warning", "VARNING" in n, n)
check("nothing seen: said so", reach_note(None, now) == "okänt hur långt bakåt")
check("an odd date is shown, not fatal", reach_note("igår", now) == "bakåt till igår")

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
