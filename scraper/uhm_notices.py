"""
Every advertised procurement in Sweden, from Upphandlingsmyndigheten's open
statistics.

Every registered advertising database reports each notice to
Upphandlingsmyndigheten (lag 2019:668), and the agency publishes them as open
data: one row per notice, from all five databases (Mercell, e-Avrop,
KommersAnnons, Clira, Konstpool), above and below the EU thresholds. 88,211
notices for 2021–2025 (June 2026). It is published once a year with no
deadline and no link to the notice, so it is no source of tenders to bid on.
What it gives:

  * What the live sources miss. Of 18,015 notices in 2025, about 2,100 (12 %)
    were below the thresholds in databases read nowhere here — e-Avrop, whose
    robots.txt forbids automated reading, Clira and Konstpool. app/coverage.py
    measures it from this table.
  * Each buyer's procurements in every database, not just those seen live.

Same handling as scraper/uhm_contracts.py: each run asks for the file and stops
at the headers when Last-Modified and the length are those already imported;
otherwise it reads the whole file (69 MB), upserts every row and deletes the
rows the new file no longer has — only if the new file is at least half the
size of the old, so a cut-off download cannot empty the table.

Terms: free to use with the source, the date and the period named; a
processed figure must say who processed it: "Uppgifter: Upphandlingsmyndigheten
| Bearbetning: Agentanbud" (mcp_shared.UHM_ATTRIBUTION).

Dataset: "Antal upphandlingar. Statistik om annonserade upphandlingar i
Sverige", catalog.upphandlingsmyndigheten.se.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
from typing import Iterable, Optional

import httpx

from app.db import connect, init_db, log_sync
from app.suppliers import could_be_person
from scraper.uhm_contracts import _digits, _known, _user_agent

LOG = logging.getLogger(__name__)

SOURCE = "uhm_notices"
CSV_URL = "https://catalog.upphandlingsmyndigheten.se/store/12/resource/255"
BATCH = 5000
# Part of the version an unchanged file is recognised by: raise it when the
# rules for reading a row change, so the next run imports the same file again.
IMPORT_RULES = 1

_UPSERT = """
INSERT INTO uhm_notices (procurement_id, year, published, title, buyer_name, buyer_key,
    buyer_orgnr, buyer_sector, cpv, category, kind, procedure, ad_database, above_threshold, run)
VALUES (:procurement_id, :year, :published, :title, :buyer_name, :buyer_key,
    :buyer_orgnr, :buyer_sector, :cpv, :category, :kind, :procedure, :ad_database,
    :above_threshold, :run)
ON CONFLICT(procurement_id) DO UPDATE SET
    year = excluded.year, published = excluded.published, title = excluded.title,
    buyer_name = excluded.buyer_name, buyer_key = excluded.buyer_key,
    buyer_orgnr = excluded.buyer_orgnr, buyer_sector = excluded.buyer_sector, cpv = excluded.cpv,
    category = excluded.category, kind = excluded.kind, procedure = excluded.procedure,
    ad_database = excluded.ad_database, above_threshold = excluded.above_threshold, run = excluded.run
"""


def map_row(row: dict, run: int) -> Optional[dict]:
    """One CSV row as a uhm_notices row, or None without an Upphandlings-ID.

    Buyers are public bodies, but the buyer's number is still dropped when it
    could be a personal identity number — nothing that could be one is kept.
    """
    pid = (row.get("Upphandlings-ID") or "").strip()
    if not pid:
        return None
    buyer = _known(row.get("Namn för köpare"))
    orgnr = _digits(row.get("Organisationsnummer för köpare"))
    if could_be_person(orgnr):
        orgnr = None
    directive = (row.get("Direktivstyrd") or "").strip()
    year = _digits(row.get("År"))
    return {
        "procurement_id": pid,
        "year": int(year) if year else None,
        "published": _known(row.get("Publiceringsdatum")),
        "title": _known(row.get("Upphandlingens titel")),
        "buyer_name": buyer, "buyer_key": buyer.casefold() if buyer else None,
        "buyer_orgnr": orgnr,
        "buyer_sector": _known(row.get("Sektor för köpare")),
        "cpv": (_digits((row.get("Huvudsaklig CPV-kod") or "").split(" ")[0]) or "")[:8] or None,
        "category": _known(row.get("Upphandlingsslag")),
        "kind": _known(row.get("Typ av avtal")),
        "procedure": _known(row.get("Förfarande")),
        "ad_database": _known(row.get("Annonsdatabas")),
        "above_threshold": 1 if directive == "Direktivstyrd" else 0 if directive == "Inte direktivstyrd" else None,
        "run": run,
    }


def import_rows(conn, rows: Iterable[dict], run: int) -> int:
    """Upsert the CSV rows; commit per batch so the write lock is held briefly."""
    n, batch = 0, []
    for row in rows:
        m = map_row(row, run)
        if m is None:
            continue
        batch.append(m)
        if len(batch) >= BATCH:
            conn.executemany(_UPSERT, batch)
            conn.commit()
            n += len(batch)
            batch = []
    if batch:
        conn.executemany(_UPSERT, batch)
        conn.commit()
        n += len(batch)
    return n


def _state(conn) -> Optional[str]:
    r = conn.execute("SELECT value FROM source_state WHERE source = ?", (SOURCE,)).fetchone()
    return r[0] if r else None


def run(db_path: str, url: str = CSV_URL, client: Optional[httpx.Client] = None) -> int:
    init_db(db_path)
    conn = connect(db_path)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True,
                                    headers={"User-Agent": _user_agent()})
    try:
        before = conn.execute("SELECT COUNT(*) FROM uhm_notices").fetchone()[0]
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            version = (f"{IMPORT_RULES}|{resp.headers.get('last-modified', '')}"
                       f"|{resp.headers.get('content-length', '')}")
            if before and version == _state(conn):
                log_sync(conn, SOURCE, "ok", 0, f"oförändrad fil ({version.split('|')[1]}), {before} annonser")
                return 0
            # To a temporary file first: a CSV cell may hold a line break.
            with tempfile.TemporaryFile() as tmp:
                for chunk in resp.iter_bytes():
                    tmp.write(chunk)
                tmp.seek(0)
                text = io.TextIOWrapper(tmp, encoding="utf-8-sig", newline="")
                run_id = (conn.execute("SELECT MAX(run) FROM uhm_notices").fetchone()[0] or 0) + 1
                n = import_rows(conn, csv.DictReader(text, delimiter=";"), run_id)
        if n < before / 2:
            log_sync(conn, SOURCE, "error", n,
                     f"filen gav {n} rader mot {before} förut — gamla rader behålls")
            return n
        removed = conn.execute("DELETE FROM uhm_notices WHERE run != ?", (run_id,)).rowcount
        conn.execute("INSERT INTO source_state (source, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                     "ON CONFLICT(source) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (SOURCE, version))
        conn.commit()
        years = conn.execute("SELECT MIN(year), MAX(year) FROM uhm_notices").fetchone()
        log_sync(conn, SOURCE, "ok", n,
                 f"Upphandlingsmyndigheten: {n} annonserade upphandlingar {years[0]}–{years[1]}, "
                 f"{removed} borttagna")
        return n
    except Exception as exc:
        LOG.exception("uhm_notices failed")
        log_sync(conn, SOURCE, "error", 0, str(exc))
        raise
    finally:
        if own:
            client.close()
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    print(run(os.environ.get("DB_PATH", "/data/application.db")))
