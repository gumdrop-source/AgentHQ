-- 001_users.sql — Users table and the first-admin bootstrap.
--
-- The first user that signs up becomes role='admin' (handled in app code).
-- Every subsequent signup defaults to role='user' and only an admin can
-- promote. Empty users table = open signup screen.

CREATE TABLE users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,
  display_name  TEXT    NOT NULL,
  role          TEXT    NOT NULL CHECK (role IN ('admin', 'user')) DEFAULT 'user',
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_users_email ON users(email);
