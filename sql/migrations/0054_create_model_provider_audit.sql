CREATE TABLE model_provider_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER,
    provider_name TEXT NOT NULL,
    action TEXT NOT NULL,
    old_values_json TEXT NOT NULL DEFAULT '{}',
    new_values_json TEXT NOT NULL DEFAULT '{}',
    modified_by_user_id INTEGER,
    modified_by_username TEXT NOT NULL,
    created_at TEXT NOT NULL
);
