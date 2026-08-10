CREATE TABLE photo_lifecycle_operations (
    operation_id TEXT PRIMARY KEY CHECK (length(trim(operation_id)) > 0),
    action TEXT NOT NULL CHECK (action IN ('soft_delete', 'restore')),
    photo_id INTEGER NOT NULL UNIQUE CHECK (photo_id > 0),
    expected_version INTEGER NOT NULL CHECK (expected_version > 0),
    source_path TEXT NOT NULL CHECK (length(trim(source_path)) > 0),
    destination_path TEXT NOT NULL CHECK (length(trim(destination_path)) > 0),
    admin_user_id INTEGER NOT NULL,
    admin_username TEXT NOT NULL,
    lease_owner TEXT NOT NULL CHECK (length(trim(lease_owner)) > 0),
    lease_expires_at TEXT NOT NULL CHECK (length(trim(lease_expires_at)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
