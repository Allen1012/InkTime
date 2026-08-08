CREATE TABLE photo_audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id          INTEGER NOT NULL,
    admin_user_id     INTEGER NOT NULL,
    admin_username    TEXT NOT NULL,
    action            TEXT NOT NULL,
    old_values_json   TEXT NOT NULL,
    new_values_json   TEXT NOT NULL,
    batch_id          TEXT,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (photo_id) REFERENCES photo_scores(id),
    FOREIGN KEY (admin_user_id) REFERENCES admin_users(id)
);
