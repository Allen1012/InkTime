CREATE TABLE admin_login_failures (
    attempt_key     TEXT NOT NULL,
    failed_at_epoch INTEGER NOT NULL,
    attempt_nonce   TEXT NOT NULL,
    PRIMARY KEY (attempt_key, failed_at_epoch, attempt_nonce)
);
