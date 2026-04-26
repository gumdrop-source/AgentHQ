-- 002_sessions.sql — Login sessions.
--
-- Session ID lives in an httpOnly signed cookie. Server looks up the row
-- on every authenticated request to find the user. CASCADE on user delete
-- means deleting a user logs out all their devices.

CREATE TABLE sessions (
  id          TEXT    PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  expires_at  TEXT    NOT NULL
);

CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);
