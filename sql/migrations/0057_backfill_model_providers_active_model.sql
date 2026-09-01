UPDATE model_providers SET active_model = TRIM(CASE WHEN INSTR(model_name, ';') > 0 THEN SUBSTR(model_name, 1, INSTR(model_name, ';') - 1) ELSE model_name END) WHERE TRIM(active_model) = '';
