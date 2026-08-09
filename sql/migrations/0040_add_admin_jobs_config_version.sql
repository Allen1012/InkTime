ALTER TABLE admin_jobs
ADD COLUMN config_version INTEGER NOT NULL DEFAULT 0 CHECK (config_version >= 0);
