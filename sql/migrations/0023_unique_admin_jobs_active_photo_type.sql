CREATE UNIQUE INDEX uq_admin_jobs_active_photo_type ON admin_jobs (photo_id, job_type) WHERE status IN ('pending', 'running');
