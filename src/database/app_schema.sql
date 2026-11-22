-- Supporting tables for background jobs and API caching.

CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY,
 paper_id INTEGER NOT NULL REFERENCES papers(id),
 status TEXT NOT NULL,
 stage TEXT NOT NULL,
 error TEXT,
 created_at TEXT NOT NULL,
 finished_at TEXT
);
CREATE TABLE IF NOT EXISTS discovery_cache (
 provider TEXT NOT NULL,
 query TEXT NOT NULL,
 fetched_at REAL NOT NULL,
 results TEXT NOT NULL,
 PRIMARY KEY(provider, query)
);
CREATE TABLE IF NOT EXISTS discovery_provider_state (
 provider TEXT PRIMARY KEY,
 retry_at REAL NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0,
 lease_token TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_resources_paper ON resources(paper_id);
CREATE INDEX IF NOT EXISTS idx_papers_score ON papers(score);
