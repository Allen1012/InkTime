UPDATE display_artifact_state SET desired_generation = CASE WHEN blocked = 1 THEN generation + 1 ELSE generation END WHERE id = 1;
