-- Durable import-job records: job status survives server restarts.
CREATE TABLE IF NOT EXISTS import_jobs (
    job_id TEXT PRIMARY KEY,                  -- short uuid, matches previous in-memory ids
    temp_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    account_type TEXT NOT NULL,
    year INTEGER,
    start_month INTEGER,
    target_account_id INTEGER REFERENCES accounts(id),
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','error')),
    inserted INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER,
    reconciled INTEGER,                       -- 0/1, null until done
    diff REAL,
    error TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON import_jobs(status);
