ALTER TABLE photo_scores ADD COLUMN is_included INTEGER NOT NULL DEFAULT 0 CHECK (is_included IN (0, 1));
