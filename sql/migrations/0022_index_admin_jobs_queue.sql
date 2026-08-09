CREATE INDEX idx_admin_jobs_queue ON admin_jobs (status, priority DESC, lease_expires_at, created_at, id);
