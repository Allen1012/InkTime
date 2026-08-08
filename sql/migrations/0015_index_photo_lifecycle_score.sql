CREATE INDEX idx_photo_scores_lifecycle_score ON photo_scores (is_deleted, analysis_status, memory_score DESC, id DESC);
