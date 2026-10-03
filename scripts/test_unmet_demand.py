#!/usr/bin/env python3
"""What may and may not appear in "Efterfrågan utan träff".

Every case here is a way the old query manufactured a data gap: it trusted a
logged `results = 0` on its own, so a search the status filter emptied, a
search narrowed by a source filter, and a crawler's walk all read as "we have
no data for this". The panel is shown to sponsors, so a false gap is a false
claim about the corpus.

Run: python3 scripts/test_unmet_demand.py
"""
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.insights import usage_summary  # noqa: E402

# (term, meta, should_be_reported, why this case exists)
CASES = [
    ("kvantkryptografi",
     {"bot": 0, "results": 0, "results_all": 0},
     True, "nothing in the corpus and no filter in the way — a real gap"),

    ("ventilation",
     {"bot": 0, "results": 0, "results_all": 7, "status": "open"},
     False, "matches existed, they had all closed — the filter emptied it"),

    ("ventilation",
     {"bot": 0, "results": 0, "status": "open"},
     False, "logged before results_all existed; live corpus has the data"),

    ("kvantkryptografi",
     {"bot": 0, "results": 0, "results_all": 0, "source": "mercell"},
     False, "narrowed to one source — says nothing about the term"),

    ("kvantkryptografi",
     {"bot": 0, "results": 0, "results_all": 0, "authority": "Trafikverket"},
     False, "narrowed to one authority"),

    ("kvantkryptografi",
     {"bot": 0, "results": 0, "results_all": 0, "cpv": "48000000"},
     False, "narrowed to one CPV code"),

    ("kvantkryptografi",
     {"bot": 1, "results": 0, "results_all": 0},
     False, "a crawler walking filter links is not demand"),
]


def build() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tenders (id INTEGER PRIMARY KEY, title TEXT, description TEXT);
        CREATE TABLE usage_log (
            id INTEGER PRIMARY KEY, channel TEXT, action TEXT, query TEXT,
            meta TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE daily_counters (day TEXT, kind TEXT, n INTEGER);
        """
    )
    # The corpus answers "ventilation" and knows nothing of "kvantkryptografi".
    conn.execute("INSERT INTO tenders (title, description) VALUES (?, ?)",
                 ("Ramavtal Ventilation 2026", "OVK och injustering"))
    conn.commit()
    return conn


def main() -> int:
    failures = []
    for term, meta, expected, why in CASES:
        conn = build()
        conn.execute(
            "INSERT INTO usage_log (channel, action, query, meta) VALUES (?,?,?,?)",
            ("browser", "search", term, json.dumps(meta)))
        conn.commit()
        reported = {u["term"] for u in usage_summary(conn)["unmet_demand"]}
        got = term in reported
        ok = got == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {term:18} "
              f"{'reported' if got else 'not reported':14} — {why}")
        if not ok:
            failures.append((term, why, expected, got))
        conn.close()

    # A term the corpus still cannot answer must survive, so the panel does not
    # simply go empty: a fix that reports nothing would pass every case above.
    conn = build()
    conn.executemany(
        "INSERT INTO usage_log (channel, action, query, meta) VALUES (?,?,?,?)",
        [("browser", "search", "kvantkryptografi",
          json.dumps({"bot": 0, "results": 0, "results_all": 0}))] * 3)
    conn.commit()
    rows = usage_summary(conn)["unmet_demand"]
    ok = rows and rows[0]["term"] == "kvantkryptografi" and rows[0]["n"] == 3
    print(f"  {'PASS' if ok else 'FAIL'}  {'counts survive':18} "
          f"{rows[:1]} — a real gap keeps its tally")
    if not ok:
        failures.append(("kvantkryptografi", "tally", 3, rows[:1]))
    conn.close()

    if failures:
        print(f"\n{len(failures)} case(s) failed")
        return 1
    print(f"\nAlla {len(CASES) + 1} fall passerade")
    return 0


if __name__ == "__main__":
    sys.exit(main())
