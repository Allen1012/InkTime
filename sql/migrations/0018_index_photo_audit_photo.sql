CREATE INDEX idx_photo_audit_log_photo_created ON photo_audit_log (photo_id, created_at DESC);
