ALTER TABLE photo_scores ADD COLUMN analysis_status TEXT NOT NULL DEFAULT 'legacy' CHECK (analysis_status IN ('legacy', 'pending', 'running', 'succeeded', 'failed'));
