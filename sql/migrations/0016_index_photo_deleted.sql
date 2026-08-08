CREATE INDEX idx_photo_scores_deleted ON photo_scores (is_deleted, deleted_at DESC, id DESC);
