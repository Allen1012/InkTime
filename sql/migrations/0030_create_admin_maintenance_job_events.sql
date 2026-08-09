CREATE TABLE admin_maintenance_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    admin_user_id INTEGER,
    worker_id TEXT,
    reason_code TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES admin_maintenance_jobs(id)
);
