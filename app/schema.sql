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
    winner_name TEXT,                      -- JSON list of awarded suppliers (ted_awards only)
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

-- Knowledge base — sustainability criteria + Q&A from Upphandlingsmyndigheten.
-- Separate from tenders: these are reference material, not active procurements.
-- Same shape: (source_system, source_id) for upsert deduplication.
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,           -- 'criteria' | 'questions'
    source_id TEXT NOT NULL,
    url TEXT,
    title TEXT,
    category TEXT,                          -- primary category (from breadcrumb level 3 or first tag)
    subcategory TEXT,                       -- secondary
    tags TEXT,                              -- JSON list of all categories / tags
    excerpt TEXT,                            -- short summary / question text
    body TEXT,                              -- full text if available (currently same as excerpt)
    raw_json TEXT,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_system, source_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge(source_system);
CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_subcategory ON knowledge(subcategory);

-- Blog — agent-authored posts about Swedish public procurement.
-- An AI agent (with the admin key) creates posts via MCP/REST; the public
-- reads them. body_md is Markdown, rendered to HTML at request time.
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,              -- URL slug, e.g. 'nya-lou-troskelvarden-2026'
    title TEXT NOT NULL,
    summary TEXT,                           -- short excerpt (list view + og:description)
    body_md TEXT NOT NULL,                  -- Markdown source
    tags TEXT,                              -- JSON list of tags
    author TEXT DEFAULT 'Agentanbud AI',
    status TEXT DEFAULT 'published',        -- 'published' | 'draft'
    published_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_posts_status_pub ON posts(status, published_at DESC);

-- Engagement events — privacy-preserving: no IP, no cookies, no PII.
-- 'view' = post page loaded; 'read' = reader scrolled to the end.
CREATE TABLE IF NOT EXISTS post_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                     -- 'view' | 'read'
    day TEXT,                               -- YYYY-MM-DD (for daily aggregation)
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_post_events ON post_events(post_id, kind);
