CREATE TABLE display_artifact_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
    generation INTEGER NOT NULL DEFAULT 0,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    maintenance_job_id INTEGER,
    FOREIGN KEY (maintenance_job_id) REFERENCES admin_maintenance_jobs(id)
);
