CREATE UNIQUE INDEX uq_admin_maintenance_jobs_active_type ON admin_maintenance_jobs (job_type) WHERE status IN ('pending', 'running');
