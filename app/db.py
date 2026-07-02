"""SQLite helpers — connection, schema bootstrap, upsert, log writer."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def resolve_db_path(requested: str | Path) -> Path:
    """Return the actual DB path to use.

    The app's DB lives in a Docker volume and the filename should never
    change across project renames — it's just ``application.db``.

    If somehow the configured file doesn't exist but a legacy name is
    present in the same directory, use that instead. This is a one-way
    safety net for users migrating from older installs.
    """
    p = Path(requested)
    if p.exists():
        return p
    # Legacy fallback chain (only consulted if the configured file is missing)
    for legacy_name in ("opentender.db", "agentanbud.db", "upphandling.db"):
        legacy = p.parent / legacy_name
        if legacy.exists():
            LOG.info("DB %s missing, using legacy %s", p, legacy)
            return legacy
    return p


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with Row factory + WAL mode for safe concurrent reads."""
    p = resolve_db_path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    # WAL lets FastAPI readers run concurrently with the scraper writer.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create schema if missing. Idempotent."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def upsert_tender(conn: sqlite3.Connection, t: dict) -> None:
    """Insert or replace a tender keyed on (source_system, source_id)."""
    # Normalise CPV list to JSON string
    if "cpv_codes" in t and not isinstance(t["cpv_codes"], str):
        t["cpv_codes"] = json.dumps(t["cpv_codes"], ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO tenders (
            source_system, source_id, tender_url, title, authority,
            cpv_codes, deadline, published_at, description, value,
            procedure, contract_type, document_type, region, raw_json
        ) VALUES (
            :source_system, :source_id, :tender_url, :title, :authority,
            :cpv_codes, :deadline, :published_at, :description, :value,
            :procedure, :contract_type, :document_type, :region, :raw_json
        )
        ON CONFLICT(source_system, source_id) DO UPDATE SET
            tender_url=excluded.tender_url,
            title=excluded.title,
            authority=excluded.authority,
            cpv_codes=excluded.cpv_codes,
            deadline=excluded.deadline,
            published_at=excluded.published_at,
            description=excluded.description,
            value=excluded.value,
            procedure=excluded.procedure,
            contract_type=excluded.contract_type,
            document_type=excluded.document_type,
            region=excluded.region,
            raw_json=excluded.raw_json,
            fetched_at=CURRENT_TIMESTAMP
        """,
        t,
    )


def log_sync(conn: sqlite3.Connection, source: str, status: str, count: int, message: str = "") -> None:
    """Record a sync run for the dashboard's recent-runs view."""
    conn.execute(
        "INSERT INTO sync_log (source, status, count, message) VALUES (?, ?, ?, ?)",
        (source, status, count, message[:500]),
    )
    conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
