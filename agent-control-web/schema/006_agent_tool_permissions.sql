-- 006_agent_tool_permissions.sql — Junction table for per-agent + per-tool grants.
--
-- One row per (agent, mcp_server, tool) tuple that's been granted.
-- Absence of a row = denied. This is the table that backs the
-- "Daisy gets MYOB → accounts ✓ and bank_balance ✓ but NOT reconciliation"
-- semantics.
--
-- Whenever this table changes for an agent, the agent's settings.json is
-- regenerated and the systemd service is restarted to pick up the new
-- permission set.

CREATE TABLE agent_tool_permissions (
  agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  mcp_server_id   TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  tool_name       TEXT NOT NULL,
  granted_at      TEXT NOT NULL DEFAULT (datetime('now')),
  granted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  PRIMARY KEY (agent_id, mcp_server_id, tool_name)
);

CREATE INDEX idx_atp_agent ON agent_tool_permissions(agent_id);
CREATE INDEX idx_atp_mcp ON agent_tool_permissions(mcp_server_id);
