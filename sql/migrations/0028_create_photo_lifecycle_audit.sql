CREATE TABLE photo_lifecycle_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    photo_id INTEGER NOT NULL,
    path_snapshot TEXT,
    admin_user_id INTEGER,
    admin_username TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
