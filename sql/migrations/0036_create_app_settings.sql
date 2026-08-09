CREATE TABLE app_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    settings_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    modified_by_user_id INTEGER,
    modified_by_username TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
