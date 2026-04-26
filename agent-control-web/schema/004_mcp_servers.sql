-- 004_mcp_servers.sql — MCP server registry (the "Integrations" catalog).
--
-- One row per integration the platform knows about. 'inactive' means the
-- code is on disk under /opt/agents/tools/<id>/ but no credentials have
-- been injected yet; 'active' means credentials are in the systemd-creds
-- vault and agents can be granted access to it.

CREATE TABLE mcp_servers (
  id            TEXT NOT NULL PRIMARY KEY,
  title         TEXT NOT NULL,
  description   TEXT,
  status        TEXT NOT NULL CHECK (status IN ('active', 'inactive')) DEFAULT 'inactive',
  activated_at  TEXT
);
