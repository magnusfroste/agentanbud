"""
Awarded contracts in Sweden, from Upphandlingsmyndigheten's open statistics.

Every registered advertising database reports each notice to
Upphandlingsmyndigheten (lag 2019:668), and the agency publishes the
contracts as open data: one row per supplier per lot, with the title, the
buyer and the supplier by name and organisation number, the main CPV code,
the procedure, the database the notice came from and whether it was
appealed. 100,864 rows for 2021–2024 (October 2026), 23,030 of them from
e-Avrop, which forbids scraping, and a quarter below the EU thresholds,
where TED says nothing. Until now the only winners here came from TED.

It is published once a year as one CSV file (113 MB, about 8 s). Each run
asks for it and stops at the headers when Last-Modified and the length are
those of the file already imported; otherwise it reads the whole file,
upserts every row and then deletes the rows the new file no longer has —
only if the new file is at least half the size of the old, so a cut-off
download cannot empty the table.

Terms: free to use with the source named. A processed figure must say who
processed it: "Uppgifter: Upphandlingsmyndigheten | Bearbetning: Agentanbud"
(mcp_shared.UHM_ATTRIBUTION), wherever these figures are shown.

Dataset: "Antal kontrakterade anbud med leverantörer. Statistik om
annonserade upphandlingar i Sverige", catalog.upphandlingsmyndigheten.se.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import tempfile
from typing import Iterable, Optional

import httpx

from app.db import connect, init_db, log_sync

LOG = logging.getLogger(__name__)

SOURCE = "uhm_contracts"
CSV_URL = "https://catalog.upphandlingsmyndigheten.se/store/12/resource/239"
BATCH = 5000

_UPSERT = """
INSERT INTO contracts (lot_id, supplier_key, supplier_name, supplier_orgnr, supplier_size,
    procurement_id, title, buyer_name, buyer_key, buyer_orgnr, buyer_sector, cpv, published,
    year, kind, procedure, ad_database, above_threshold, appealed, run)
VALUES (:lot_id, :supplier_key, :supplier_name, :supplier_orgnr, :supplier_size,
    :procurement_id, :title, :buyer_name, :buyer_key, :buyer_orgnr, :buyer_sector, :cpv, :published,
    :year, :kind, :procedure, :ad_database, :above_threshold, :appealed, :run)
ON CONFLICT(lot_id, supplier_key) DO UPDATE SET
    supplier_name = excluded.supplier_name, supplier_orgnr = excluded.supplier_orgnr,
    supplier_size = excluded.supplier_size, procurement_id = excluded.procurement_id,
    title = excluded.title, buyer_name = excluded.buyer_name, buyer_key = excluded.buyer_key,
    buyer_orgnr = excluded.buyer_orgnr, buyer_sector = excluded.buyer_sector, cpv = excluded.cpv,
    published = excluded.published, year = excluded.year, kind = excluded.kind,
    procedure = excluded.procedure, ad_database = excluded.ad_database,
    above_threshold = excluded.above_threshold, appealed = excluded.appealed, run = excluded.run
"""


def _user_agent() -> str:
    return os.environ.get("USER_AGENT", "agentanbud/0.1")


def _known(value: Optional[str]) -> Optional[str]:
    """The cell, or None for the agency's "Uppgift saknas" and blanks."""
    v = (value or "").strip()
    return None if not v or v.lower() == "uppgift saknas" else v


def _digits(value: Optional[str]) -> Optional[str]:
    d = re.sub(r"\D", "", value or "")
    return d or None


def map_row(row: dict, run: int, anon_seq: dict) -> Optional[dict]:
    """One CSV row as a contracts row, or None when it has no lot id.

    A sole trader's name and number are anonymised to "PERSONUPPGIFT" /
    "Personuppgift": the row is kept, so the contract still counts, but with
    no name, and keyed per lot so two anonymous suppliers stay two.
    """
    lot = (row.get("Anbudsområdes-ID") or "").strip()
    if not lot:
        return None
    name = _known(row.get("Namn för leverantör"))
    orgnr = _digits(row.get("Organisationsnummer för leverantör"))
    if name and name.upper() == "PERSONUPPGIFT":
        name, orgnr = None, None
    if orgnr:
        key = orgnr
    elif name:
        key = "name:" + name.casefold()
    else:
        anon_seq[lot] = anon_seq.get(lot, 0) + 1
        key = f"anon:{anon_seq[lot]}"
    directive = (row.get("Direktivstyrd") or "").strip()
    appealed = (row.get("Överprövad") or "").strip()
    buyer = _known(row.get("Namn för köpare"))
    year = _digits(row.get("År"))
    return {
        "lot_id": lot, "supplier_key": key, "supplier_name": name, "supplier_orgnr": orgnr,
        "supplier_size": _known(row.get("Företagsstorlek för leverantör")),
        "procurement_id": _known(row.get("Upphandlings-ID")),
        "title": _known(row.get("Upphandlingens titel")),
        "buyer_name": buyer, "buyer_key": buyer.casefold() if buyer else None,
        "buyer_orgnr": _digits(row.get("Organisationsnummer för köpare")),
        "buyer_sector": _known(row.get("Sektor för köpare")),
        "cpv": (_digits((row.get("Huvudsaklig CPV-kod") or "").split(" ")[0]) or "")[:8] or None,
        "published": _known(row.get("Publiceringsdatum")),
        "year": int(year) if year else None,
        "kind": _known(row.get("Typ av avtal")),
        "procedure": _known(row.get("Förfarande")),
        "ad_database": _known(row.get("Annonsdatabas")),
        "above_threshold": 1 if directive == "Direktivstyrd" else 0 if directive == "Inte direktivstyrd" else None,
        "appealed": 1 if appealed == "Överprövad" else 0 if appealed == "Inte överprövad" else None,
        "run": run,
    }


def import_rows(conn, rows: Iterable[dict], run: int) -> int:
    """Upsert the CSV rows; commit per batch so the write lock is held briefly."""
    n, batch, anon_seq = 0, [], {}
    for row in rows:
        m = map_row(row, run, anon_seq)
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
        before = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            version = f"{resp.headers.get('last-modified', '')}|{resp.headers.get('content-length', '')}"
            if before and version == _state(conn):
                log_sync(conn, SOURCE, "ok", 0, f"oförändrad fil ({version.split('|')[0]}), {before} kontrakt")
                return 0
            # To a temporary file first: a CSV cell may hold a line break,
            # so the rows cannot be split from the stream line by line.
            with tempfile.TemporaryFile() as tmp:
                for chunk in resp.iter_bytes():
                    tmp.write(chunk)
                tmp.seek(0)
                text = io.TextIOWrapper(tmp, encoding="utf-8-sig", newline="")
                # One more than the last import, not the clock: two imports in
                # the same second would otherwise share an id, and the second
                # could not tell its own rows from the first one's.
                run_id = (conn.execute("SELECT MAX(run) FROM contracts").fetchone()[0] or 0) + 1
                n = import_rows(conn, csv.DictReader(text, delimiter=";"), run_id)
        if n < before / 2:
            log_sync(conn, SOURCE, "error", n,
                     f"filen gav {n} rader mot {before} förut — gamla rader behålls")
            return n
        removed = conn.execute("DELETE FROM contracts WHERE run != ?", (run_id,)).rowcount
        conn.execute("INSERT INTO source_state (source, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                     "ON CONFLICT(source) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (SOURCE, version))
        conn.commit()
        years = conn.execute("SELECT MIN(year), MAX(year) FROM contracts").fetchone()
        log_sync(conn, SOURCE, "ok", n,
                 f"Upphandlingsmyndigheten: {n} kontrakterade anbud {years[0]}–{years[1]}, {removed} borttagna")
        return n
    except Exception as exc:
        LOG.exception("uhm_contracts failed")
        log_sync(conn, SOURCE, "error", 0, str(exc))
        raise
    finally:
        if own:
            client.close()
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    print(run(os.environ.get("DB_PATH", "/data/application.db")))
