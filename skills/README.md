# Skills

Skill playbooks — markdown documents that compose tools to accomplish
a task. Bootstrap phase 30 syncs them to `/opt/agents/skills/`.

Skills here are **generic** (e.g. "send a calendar invite", "draft an
email reply"). Business-specific skills (e.g. lead-research playbooks
that know our CRM schema) live in the agent's own `skills/` directory,
not in this shared library.

A skill file must:

- Be self-contained markdown with a clear "Goal" / "Steps" / "Output" structure
- Reference tools by name (e.g. `m365.outlook_email_search`), not by file path
- Not embed credentials, identifiers, or organisation-specific details
