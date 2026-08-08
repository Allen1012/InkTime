ALTER TABLE photo_scores ADD COLUMN content_sha256 TEXT CHECK (content_sha256 IS NULL OR length(content_sha256) = 64);
