-- 005_tools.sql — Tool inventory (cache of /opt/agents/tools/<id>/tool.json).
--
-- One row per tool exposed by an MCP server. Refreshed on server startup
-- (and on demand when activating an integration) by reading each tool.json.
-- Source of truth is the manifest file; the DB is just for fast lookups
-- and to back the per-agent permission matrix UI.

CREATE TABLE tools (
  mcp_server_id  TEXT    NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
  name           TEXT    NOT NULL,
  description    TEXT,
  destructive    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (mcp_server_id, name)
);
