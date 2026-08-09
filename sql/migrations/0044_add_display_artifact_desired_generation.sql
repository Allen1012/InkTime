ALTER TABLE display_artifact_state ADD COLUMN desired_generation INTEGER NOT NULL DEFAULT 0 CHECK (desired_generation >= 0);
