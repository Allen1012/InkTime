UPDATE photo_scores SET is_included = 1 WHERE analysis_status IN ('legacy', 'succeeded');
