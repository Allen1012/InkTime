CREATE INDEX idx_photo_scores_lifecycle_date ON photo_scores (is_deleted, analysis_status, exif_datetime DESC, id DESC);
