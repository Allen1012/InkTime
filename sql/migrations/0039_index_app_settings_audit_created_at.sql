CREATE INDEX idx_app_settings_audit_created_at
ON app_settings_audit(created_at DESC, id DESC);
