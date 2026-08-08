CREATE INDEX idx_photo_audit_log_admin_created ON photo_audit_log (admin_user_id, created_at DESC);
