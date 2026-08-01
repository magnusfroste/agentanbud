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
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent column migrations to an existing tenders table.

    CREATE TABLE IF NOT EXISTS never adds columns to a table that already
    exists, so new columns are added here via ALTER TABLE guarded by a
    PRAGMA check. Cheap enough to run on every init_db().
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tenders)")}
    if "winner_name" not in cols:
        conn.execute("ALTER TABLE tenders ADD COLUMN winner_name TEXT")
        LOG.info("migrated: added tenders.winner_name column")


def upsert_tender(conn: sqlite3.Connection, t: dict) -> None:
    """Insert or replace a tender keyed on (source_system, source_id)."""
    # Normalise CPV list to JSON string
    if "cpv_codes" in t and not isinstance(t["cpv_codes"], str):
        t["cpv_codes"] = json.dumps(t["cpv_codes"], ensure_ascii=False)
    # winner_name is optional (only ted_awards sets it); normalise list→JSON
    wn = t.get("winner_name")
    if wn is not None and not isinstance(wn, str):
        wn = json.dumps(wn, ensure_ascii=False)
    t = {**t, "winner_name": wn}
    conn.execute(
        """
        INSERT INTO tenders (
            source_system, source_id, tender_url, title, authority,
            cpv_codes, deadline, published_at, description, value,
            procedure, contract_type, document_type, region, winner_name, raw_json
        ) VALUES (
            :source_system, :source_id, :tender_url, :title, :authority,
            :cpv_codes, :deadline, :published_at, :description, :value,
            :procedure, :contract_type, :document_type, :region, :winner_name, :raw_json
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
            winner_name=excluded.winner_name,
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


def upsert_knowledge(conn: sqlite3.Connection, k: dict) -> None:
    """Insert or replace a knowledge item keyed on (source_system, source_id).

    Same pattern as upsert_tender: stable unique key, replace on conflict.
    Used for sustainability criteria and Q&A portal data.
    """
    if "tags" in k and not isinstance(k["tags"], str):
        k["tags"] = json.dumps(k["tags"], ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO knowledge (
            source_system, source_id, url, title, category, subcategory,
            tags, excerpt, body, raw_json
        ) VALUES (
            :source_system, :source_id, :url, :title, :category, :subcategory,
            :tags, :excerpt, :body, :raw_json
        )
        ON CONFLICT(source_system, source_id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            category=excluded.category,
            subcategory=excluded.subcategory,
            tags=excluded.tags,
            excerpt=excluded.excerpt,
            body=excluded.body,
            raw_json=excluded.raw_json,
            fetched_at=CURRENT_TIMESTAMP
        """,
        k,
    )


def log_usage(
    conn: sqlite3.Connection,
    channel: str,
    action: str,
    query: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Record one usage event for the /analytics page.

    Best-effort: callers should not let a logging failure break the
    request they're instrumenting.
    """
    conn.execute(
        "INSERT INTO usage_log (channel, action, query, meta) VALUES (?, ?, ?, ?)",
        (channel, action, query, json.dumps(meta, ensure_ascii=False) if meta else None),
    )
    conn.commit()


def bump_daily_counter(conn: sqlite3.Connection, kind: str, day: Optional[str] = None) -> None:
    """Increment an aggregated daily counter (one row per day+kind).

    Used for high-volume, low-information events — crawler pageviews — so a
    bot storm costs one UPDATE per day instead of one INSERT per request.
    """
    d = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO daily_counters (day, kind, n) VALUES (?, ?, 1) "
        "ON CONFLICT(day, kind) DO UPDATE SET n = n + 1",
        (d, kind),
    )
    conn.commit()


def prune_logs(conn: sqlite3.Connection, days: int = 30, counter_days: int = 400) -> None:
    """Cap the append-only log tables to a retention window so they can't grow
    without bound. Reclaims disk with VACUUM when a lot was deleted.

    daily_counters is kept much longer — it's one tiny row per day, so a
    year-plus of history costs nothing.
    Best-effort; safe to call on every startup.
    """
    try:
        cutoff = f"-{int(days)} days"
        deleted = conn.execute(
            "DELETE FROM usage_log WHERE created_at < DATE('now', ?)", (cutoff,)
        ).rowcount or 0
        deleted += conn.execute(
            "DELETE FROM post_events WHERE created_at < DATE('now', ?)", (cutoff,)
        ).rowcount or 0
        conn.execute(
            "DELETE FROM daily_counters WHERE day < DATE('now', ?)",
            (f"-{int(counter_days)} days",),
        )
        conn.commit()
        if deleted > 1000:
            # SQLite keeps freed pages unless vacuumed; must run outside a
            # transaction, so commit above then VACUUM on its own.
            try:
                conn.execute("VACUUM")
                LOG.info("prune_logs: deleted %d rows, vacuumed", deleted)
            except Exception:
                LOG.warning("prune_logs: VACUUM skipped", exc_info=True)
    except Exception:
        LOG.exception("prune_logs failed")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- Blog helpers ---------------------------------------------------------

import re as _re


def slugify(text: str) -> str:
    """Make a URL-safe slug from a title. Swedish å/ä/ö → a/a/o."""
    s = (text or "").strip().lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e"), ("ü", "u")):
        s = s.replace(a, b)
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80] or "inlagg"


def unique_slug(conn: sqlite3.Connection, base: str, exclude_id: int | None = None) -> str:
    """Return `base`, or base-2, base-3… if taken by another post."""
    slug = base
    n = 1
    while True:
        row = conn.execute("SELECT id FROM posts WHERE slug = ?", (slug,)).fetchone()
        if not row or (exclude_id is not None and row[0] == exclude_id):
            return slug
        n += 1
        slug = f"{base}-{n}"


def create_post(conn: sqlite3.Connection, p: dict) -> dict:
    """Insert a new blog post. Returns {id, slug}."""
    tags = p.get("tags")
    if tags is not None and not isinstance(tags, str):
        tags = json.dumps(tags, ensure_ascii=False)
    base = p.get("slug") or slugify(p.get("title", ""))
    slug = unique_slug(conn, slugify(base))
    cur = conn.execute(
        """
        INSERT INTO posts (slug, title, summary, body_md, tags, author, status, published_at, updated_at)
        VALUES (:slug, :title, :summary, :body_md, :tags, :author, :status,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {
            "slug": slug,
            "title": p.get("title") or "(utan titel)",
            "summary": p.get("summary"),
            "body_md": p.get("body_md") or "",
            "tags": tags,
            "author": p.get("author") or "Agentanbud AI",
            "status": p.get("status") or "published",
        },
    )
    conn.commit()
    return {"id": cur.lastrowid, "slug": slug}


def update_post(conn: sqlite3.Connection, slug: str, fields: dict) -> bool:
    """Update mutable fields of a post by slug. Returns True if a row changed."""
    row = conn.execute("SELECT id FROM posts WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return False
    allowed = {"title", "summary", "body_md", "status"}
    sets, params = [], []
    for k in allowed:
        if k in fields and fields[k] is not None:
            sets.append(f"{k} = ?")
            params.append(fields[k])
    if "tags" in fields and fields["tags"] is not None:
        tags = fields["tags"]
        sets.append("tags = ?")
        params.append(tags if isinstance(tags, str) else json.dumps(tags, ensure_ascii=False))
    if not sets:
        return False
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(row[0])
    conn.execute(f"UPDATE posts SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return True


def record_post_event(conn: sqlite3.Connection, slug: str, kind: str) -> bool:
    """Log a privacy-preserving engagement event ('view' | 'read')."""
    if kind not in ("view", "read"):
        return False
    row = conn.execute("SELECT id FROM posts WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return False
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO post_events (post_id, kind, day) VALUES (?, ?, ?)",
        (row[0], kind, day),
    )
    conn.commit()
    return True
