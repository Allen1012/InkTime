ALTER TABLE admin_maintenance_jobs
ADD COLUMN config_snapshot_json TEXT NOT NULL DEFAULT '{}';
