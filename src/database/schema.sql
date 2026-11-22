-- Original paper metadata fields.
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '[]',
    year INTEGER,
    abstract TEXT NOT NULL DEFAULT '',
    paper_url TEXT,
    pdf_url TEXT,
    last_analyzed_at TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'pending'
        -- 'analyzing' supports the existing background analysis screen.
        CHECK (analysis_status IN ('pending', 'completed', 'partial', 'failed', 'analyzing')),

    -- Additional fields required by the existing library and saved report screens.
    identity TEXT UNIQUE NOT NULL,
    area TEXT NOT NULL DEFAULT 'Uncategorized',
    local_path TEXT,
    created_at TEXT NOT NULL,
    report TEXT,
    score INTEGER,

    UNIQUE (source, external_id)
);

-- Original resource fields.
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    resource_type TEXT NOT NULL
        CHECK (resource_type IN ('Code', 'Dataset', 'Documentation', 'Supplementary material', 'Unknown')),
    accessibility_status TEXT NOT NULL
        CHECK (accessibility_status IN ('Accessible', 'Inaccessible', 'Inconclusive')),
    http_status INTEGER,
    error_message TEXT,
    checked_at TEXT,

    -- Additional fields needed to retain page numbers and precise HTTP outcomes.
    status TEXT NOT NULL DEFAULT 'unchecked'
        CHECK (status IN ('unchecked', 'accessible', 'missing', 'restricted', 'unresolved', 'blocked')),
    evidence TEXT NOT NULL DEFAULT '',
    page INTEGER,
    final_url TEXT,
    note TEXT NOT NULL DEFAULT '',

    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    UNIQUE (paper_id, url)
);
