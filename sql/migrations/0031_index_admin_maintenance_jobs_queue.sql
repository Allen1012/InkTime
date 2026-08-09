CREATE INDEX idx_admin_maintenance_jobs_queue ON admin_maintenance_jobs (status, priority DESC, lease_expires_at, created_at, id);
