CREATE TABLE photo_purge_operations (
    operation_id TEXT PRIMARY KEY CHECK (length(trim(operation_id)) > 0),
    photo_id INTEGER NOT NULL UNIQUE CHECK (photo_id > 0),
    expected_version INTEGER NOT NULL CHECK (expected_version > 0),
    trash_path TEXT NOT NULL CHECK (length(trim(trash_path)) > 0),
    admin_user_id INTEGER,
    admin_username TEXT NOT NULL,
    internal INTEGER NOT NULL CHECK (internal IN (0, 1)),
    lease_owner TEXT NOT NULL CHECK (length(trim(lease_owner)) > 0),
    lease_expires_at TEXT NOT NULL CHECK (length(trim(lease_expires_at)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
