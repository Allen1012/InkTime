CREATE INDEX idx_photo_scores_content_sha256 ON photo_scores (content_sha256) WHERE content_sha256 IS NOT NULL;
