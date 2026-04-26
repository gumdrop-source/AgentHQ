-- 003_agents.sql — Agent records.
--
-- One row per agent on the host. The id matches the linux username (also
-- used for /home/<id>, agent@<id>.service, etc.). The DB row is created
-- when the wizard provisions an agent; the filesystem artefacts (home dir,
-- claude install, systemd unit) are managed by the existing bash CLI.
--
-- Status is a cached snapshot from systemctl, refreshed on dashboard reads.

CREATE TABLE agents (
  id                TEXT    PRIMARY KEY,
  display_name      TEXT    NOT NULL,
  persona           TEXT,
  telegram_chat_id  TEXT,
  owner_user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  status            TEXT
);

CREATE INDEX idx_agents_owner ON agents(owner_user_id);
