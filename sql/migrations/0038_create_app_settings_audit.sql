CREATE TABLE app_settings_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    old_version INTEGER NOT NULL CHECK (old_version >= 0),
    new_version INTEGER NOT NULL CHECK (new_version = old_version + 1),
    changed_keys_json TEXT NOT NULL,
    old_values_json TEXT NOT NULL,
    new_values_json TEXT NOT NULL,
    modified_by_user_id INTEGER,
    modified_by_username TEXT NOT NULL,
    created_at TEXT NOT NULL
);
