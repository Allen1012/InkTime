CREATE INDEX idx_photo_trash_expiry ON photo_scores (is_deleted, deleted_at, id);
