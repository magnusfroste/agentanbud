-- Agentanbud schema — mirrors the public fields of offentlig.ai's `tenders`
-- table so we can ingest Mercell / TED records directly.

CREATE TABLE IF NOT EXISTS tenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,           -- 'mercell' | 'ted' | ...
    source_id TEXT NOT NULL,               -- unique ID within source (Mercell id, TED publication-number, ...)
    tender_url TEXT,                       -- canonical deeplink to the source
    title TEXT,
    authority TEXT,                        -- contracting authority / buyer
    cpv_codes TEXT,                        -- JSON list of CPV codes
    deadline TEXT,                         -- ISO8601
    published_at TEXT,                     -- ISO8601 date
    description TEXT,
    value REAL,                            -- estimated value in SEK
    procedure TEXT,                        -- e.g. "Open procedure"
    contract_type TEXT,
    document_type TEXT,
    region TEXT,
    raw_json TEXT,                         -- full source record (for debugging)
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_system, source_id)
);

CREATE INDEX IF NOT EXISTS idx_tenders_pubdate ON tenders(published_at);
CREATE INDEX IF NOT EXISTS idx_tenders_source ON tenders(source_system);
CREATE INDEX IF NOT EXISTS idx_tenders_authority ON tenders(authority);
CREATE INDEX IF NOT EXISTS idx_tenders_deadline ON tenders(deadline);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'ok' | 'error'
    count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_synclog_source_time ON sync_log(source, run_at DESC);
