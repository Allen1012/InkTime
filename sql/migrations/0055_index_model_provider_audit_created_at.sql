CREATE INDEX idx_model_provider_audit_created_at
ON model_provider_audit(created_at DESC, id DESC);
