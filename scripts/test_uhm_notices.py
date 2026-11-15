#!/usr/bin/env python3
"""Every advertised procurement in Sweden, from Upphandlingsmyndigheten's
yearly notice statistics: imported once per file, never emptied by a cut-off
download, and used to say how much of Sweden the live sources reach
(get_stats, /api/stats) and what a buyer has advertised in every database
(get_authority). A small file shaped like the real one, served by a mock.

Run: python3 scripts/test_uhm_notices.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app import coverage  # noqa: E402
from app.db import connect  # noqa: E402
from scraper import uhm_notices  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


COLS = ("År;Antal upphandlingar, Antal;Annonsdatabas;Direktivstyrd;Förfarande;Typ av avtal;"
        "Huvudsaklig CPV-kod;Publiceringsdatum;Upphandlingens titel;Upphandlings-ID;Upphandlingsslag;"
        "Namn för köpare;Organisationsnummer för köpare;Sektor för köpare")


def line(pid, year, db, directive, title, buyer, orgnr="2120001234"):
    return ";".join([year, "1", db, directive, "Öppet förfarande", "Kontrakt",
                     "90910000 Städning", f"{year}-03-01", title, pid, "Tjänster", buyer, orgnr, "Kommun"])


ROWS = [
    line("SE1", "2025", "e-Avrop", "Inte direktivstyrd ", "Lokalvård skola", "GÖTEBORGS KOMMUN"),
    line("SE2", "2025", "e-Avrop", "Direktivstyrd", "Lokalvård sjukhus", "GÖTEBORGS KOMMUN"),
    line("MT1", "2025", "Mercell Annonsdatabas", "Inte direktivstyrd ", "Städ kontor", "GÖTEBORGS KOMMUN"),
    line("KA1", "2025", "KommersAnnons.se", "Inte direktivstyrd ", "Fönsterputs", "KINDA KOMMUN"),
    line("AC1", "2025", "Clira Annonsdatabas", "Inte direktivstyrd ", "Golvvård", "KINDA KOMMUN"),
    line("SE3", "2024", "e-Avrop", "Inte direktivstyrd ", "Lokalvård förskola", "GÖTEBORGS KOMMUN"),
    line("MT2", "2024", "Mercell Annonsdatabas", "Direktivstyrd", "Städ bibliotek", "KINDA KOMMUN",
         orgnr="1111111111"),
]


def body(rows):
    return ("﻿" + COLS + "\n" + "\n".join(rows) + "\n").encode()


served = {"body": body(ROWS), "modified": "Mon, 01 Jun 2026 09:23:16 GMT"}


def handler(request):
    return httpx.Response(200, content=served["body"],
                          headers={"last-modified": served["modified"], "content-length": str(len(served["body"]))})


# A number standing in for one that could be a personal identity number: no
# real-looking one is written into the repository, so the check is replaced
# by one that also says yes to this marker.
_real_check = uhm_notices.could_be_person
uhm_notices.could_be_person = lambda n: n == "1111111111" or _real_check(n)

client = httpx.Client(transport=httpx.MockTransport(handler))
DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
mcp_server.DB_PATH = DB

print("Import")
check("every row is imported", uhm_notices.run(DB, client=client) == 7)
check("the same file again is not read", uhm_notices.run(DB, client=client) == 0)
conn = connect(DB)
row = conn.execute("SELECT * FROM uhm_notices WHERE procurement_id = 'SE1'").fetchone()
check("the fields", row["year"] == 2025 and row["ad_database"] == "e-Avrop" and row["above_threshold"] == 0
      and row["cpv"] == "90910000" and row["category"] == "Tjänster" and row["buyer_key"] == "göteborgs kommun",
      str(dict(row)))
check("a buyer number that could be a person's is not kept",
      conn.execute("SELECT buyer_orgnr FROM uhm_notices WHERE procurement_id = 'MT2'").fetchone()[0] is None)
conn.close()

served["body"], served["modified"] = body(ROWS[:2]), "Tue, 02 Jun 2026 09:00:00 GMT"
uhm_notices.run(DB, client=client)
conn = connect(DB)
check("a file under half the size leaves the old rows",
      conn.execute("SELECT COUNT(*) FROM uhm_notices").fetchone()[0] == 7)
check("…and says so in the sync log", conn.execute(
    "SELECT status FROM sync_log WHERE source = 'uhm_notices' ORDER BY id DESC LIMIT 1").fetchone()[0] == "error")

print("\nCoverage")
cov = coverage.coverage(conn)
check("the latest year", cov["year"] == 2025 and cov["notices"] == 5, str(cov))
# Unseen: e-Avrop below the thresholds (SE1) and Clira (AC1); SE2 is on TED,
# MT1 and KA1 are read live.
check("unseen: below the thresholds in databases not read live", cov["unseen"] == 2 and cov["reached"] == 3,
      str(cov))
by = {d["database"]: d for d in cov["databases"]}
check("per database", by["e-Avrop"]["unseen"] == 1 and by["Mercell Annonsdatabas"]["unseen"] == 0
      and by["Clira Annonsdatabas"]["unseen"] == 1, str(by))
check("an earlier year on request", coverage.coverage(conn, 2024)["notices"] == 2)

c = TestClient(APP, base_url="https://testserver")
j = c.get("/api/stats").json()
check("/api/stats carries it", j["coverage"]["reached_pct"] == 60.0, str(j.get("coverage")))
text = asyncio.run(mcp_server._get_stats(conn, {}))[0].text
check("get_stats says it, with the attribution",
      "når 3 av 5" in text and "e-Avrop 1" in text and "Bearbetning: Agentanbud" in text, text)

print("\nA buyer's history")
h = coverage.buyer_history(conn, "Göteborgs Stad")
check("Göteborgs Stad is GÖTEBORGS KOMMUN", h and h["buyer"] == "GÖTEBORGS KOMMUN", str(h))
check("per year, with what the live sources miss",
      h["years"] == [{"year": 2024, "notices": 1, "unseen": 1}, {"year": 2025, "notices": 3, "unseen": 1}], str(h))
text = asyncio.run(mcp_server._get_authority(conn, {"name": "Göteborgs Stad"}))[0].text
check("get_authority gives it even with no live tenders",
      "2024: 1, 2025: 3" in text and "Varav 2 under tröskelvärdena" in text, text)
check("a name too short to match gives none", coverage.buyer_history(conn, "AB") is None)

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
