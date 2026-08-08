CREATE TABLE admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(username)) > 0),
    password_hash TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    last_login_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
