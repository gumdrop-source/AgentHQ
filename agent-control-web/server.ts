// AgentHQ setup wizard — single-file Hono server.
//
// Runs as root on localhost:5000 (config via PORT env). Wraps the bash CLIs
// (agent-control, agenthq-cred, systemctl) and presents a step-by-step
// browser flow. Server-rendered HTML, no client framework. Tailwind via CDN.
//
// Runtime: bun. Started by /etc/systemd/system/agent-control-web.service.

import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { getCookie, setCookie, deleteCookie } from "hono/cookie";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";

import {
    userCount, userByEmail, userBySession, createUser, verifyPassword,
    createSession, destroySession,
    type User,
} from "./db.ts";

const app = new Hono<{ Variables: { user: User | null } }>();
const PORT = Number(process.env.PORT ?? 5000);
const SESSION_COOKIE = "agentctl_session";

// ─── auth ─────────────────────────────────────────────────────────────────

// Public routes (no auth required). Everything else needs a logged-in user.
const PUBLIC_PATHS = new Set(["/login", "/signup", "/logout"]);

app.use("*", async (c, next) => {
    const session = getCookie(c, SESSION_COOKIE);
    const user = session ? userBySession(session) : null;
    c.set("user", user);

    const path = c.req.path;
    if (PUBLIC_PATHS.has(path) || path.startsWith("/static/")) {
        return next();
    }
    if (user) return next();

    // First-run: empty user table → kick everyone to signup
    if (userCount() === 0) return c.redirect("/signup");
    return c.redirect("/login");
});

// ─── HTML helpers ─────────────────────────────────────────────────────────

type NavKey = "agents" | "integrations" | "updates" | "settings" | null;

const layout = (title: string, body: string, active: NavKey = null, user: User | null = null) => {
    const navItem = (key: Exclude<NavKey, null>, label: string, href: string) => {
        const cls = active === key
            ? "px-3 py-1.5 rounded-md text-sm font-medium bg-slate-900 text-white"
            : "px-3 py-1.5 rounded-md text-sm font-medium text-slate-600 hover:text-slate-900";
        return `<a href="${href}" class="${cls}">${label}</a>`;
    };
    const userBlock = user ? `
        <div class="flex items-center gap-3 text-sm">
          <span class="text-slate-600">${escapeHtml(user.display_name)}${user.role === "admin" ? ` <span class="ml-1 px-1.5 py-0.5 rounded bg-amber-100 text-amber-800 text-xs">admin</span>` : ""}</span>
          <a href="/logout" class="text-slate-500 hover:text-slate-900">Sign out</a>
        </div>` : "";
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${title} · Agent Control</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@2.0.4" integrity="sha384-HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"></script>
  <style>body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}</style>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen">
  <header class="bg-white border-b border-slate-200">
    <div class="max-w-5xl mx-auto px-6 h-14 flex items-center justify-between">
      <a href="/" class="flex items-baseline gap-2">
        <span class="text-lg font-semibold tracking-tight">Agent Control</span>
        <span class="text-xs text-slate-400">on AgentHQ</span>
      </a>
      <nav class="flex items-center gap-1">
        ${navItem("agents", "Agents", "/")}
        ${navItem("integrations", "Integrations", "/integrations")}
        ${navItem("updates", "Updates", "/updates")}
        ${navItem("settings", "Settings", "/settings")}
      </nav>
      ${userBlock}
    </div>
  </header>
  <main class="max-w-5xl mx-auto px-6 py-8">
    ${body}
  </main>
</body>
</html>`;
};

const card = (inner: string) => `<div class="bg-white rounded-xl shadow-sm border border-slate-200 p-8">${inner}</div>`;

const pageHeader = (title: string, sub?: string) => `
  <h1 class="text-2xl font-semibold tracking-tight mb-1">${title}</h1>
  ${sub ? `<p class="text-slate-600 mb-6">${sub}</p>` : ""}
`;

const button = (text: string, opts: { href?: string; type?: string; intent?: "primary" | "secondary" } = {}) => {
    const intent = opts.intent ?? "primary";
    const cls = intent === "primary"
        ? "bg-slate-900 text-white hover:bg-slate-700"
        : "bg-slate-100 text-slate-900 hover:bg-slate-200";
    if (opts.href) return `<a href="${opts.href}" class="inline-block px-4 py-2 rounded-lg font-medium ${cls}">${text}</a>`;
    return `<button type="${opts.type ?? "submit"}" class="inline-block px-4 py-2 rounded-lg font-medium ${cls}">${text}</button>`;
};

const code = (s: string) => `<div class="relative">
  <pre class="bg-slate-100 rounded-lg p-3 pr-20 text-sm font-mono overflow-x-auto whitespace-pre">${escapeHtml(s)}</pre>
  <button type="button"
          onclick="(async (b) => { try { await navigator.clipboard.writeText(b.previousElementSibling.textContent); b.textContent = 'Copied!'; setTimeout(() => b.textContent = 'Copy', 1500); } catch (e) { b.textContent = 'Failed'; } })(this)"
          class="absolute top-2 right-2 px-2 py-1 text-xs font-medium bg-white border border-slate-300 rounded hover:bg-slate-50 active:bg-slate-100">Copy</button>
</div>`;

function escapeHtml(s: string): string {
    return s.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]!);
}

// ─── filesystem / state probes ────────────────────────────────────────────

function listAgents(): { name: string; status: string }[] {
    let dirs: string[];
    try {
        dirs = readdirSync("/home");
    } catch {
        return [];
    }
    return dirs
        .filter((d) => existsSync(`/home/${d}/agent.toml`))
        .map((name) => {
            const r = spawnSync("systemctl", ["is-active", `agent@${name}.service`], { encoding: "utf8" });
            return { name, status: r.stdout.trim() || "unknown" };
        });
}

function claudeAuthenticated(name: string): boolean {
    return existsSync(`/home/${name}/.claude/.credentials.json`);
}

// ─── routes ───────────────────────────────────────────────────────────────

// ─── auth routes ──────────────────────────────────────────────────────────

const authPage = (title: string, body: string) => `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title} · Agent Control</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}</style>
</head><body class="bg-slate-50 text-slate-900 min-h-screen flex items-center justify-center">
  <div class="w-full max-w-sm p-8 bg-white rounded-xl shadow-sm border border-slate-200">
    <h1 class="text-xl font-semibold tracking-tight mb-1">Agent Control</h1>
    ${body}
  </div>
</body></html>`;

app.get("/signup", (c) => {
    // Only the first-admin signup is allowed without auth. After that, signup
    // is gated to admins (handled in /admin/users — TODO).
    if (userCount() > 0) return c.redirect("/login");
    return c.html(authPage("Create admin", `
        <p class="text-sm text-slate-600 mb-6">First-time setup. This account becomes the AgentHQ admin.</p>
        <form method="POST" action="/signup" class="space-y-4">
          <div>
            <label class="block text-xs font-medium mb-1 text-slate-700">Display name</label>
            <input name="display_name" required class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
          </div>
          <div>
            <label class="block text-xs font-medium mb-1 text-slate-700">Email</label>
            <input name="email" type="email" required class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
          </div>
          <div>
            <label class="block text-xs font-medium mb-1 text-slate-700">Password</label>
            <input name="password" type="password" required minlength="8" class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
            <p class="text-xs text-slate-400 mt-1">8+ chars. Stored hashed (argon2id).</p>
          </div>
          <button type="submit" class="w-full bg-slate-900 text-white px-4 py-2 rounded-lg font-medium">Create admin</button>
        </form>
    `));
});

app.post("/signup", async (c) => {
    if (userCount() > 0) return c.redirect("/login");
    const body = await c.req.parseBody();
    const email = String(body.email ?? "").trim();
    const password = String(body.password ?? "");
    const displayName = String(body.display_name ?? "").trim();
    if (!email || password.length < 8 || !displayName) {
        return c.html(authPage("Sign up", `<p class="text-rose-600 text-sm mb-3">Missing or invalid input.</p><a href="/signup" class="underline">Back</a>`));
    }
    const id = await createUser(email, password, displayName, "admin");
    const session = createSession(id);
    setCookie(c, SESSION_COOKIE, session, {
        httpOnly: true, sameSite: "Lax", path: "/",
        maxAge: 30 * 86400,
    });
    return c.redirect("/");
});

app.get("/login", (c) => {
    if (userCount() === 0) return c.redirect("/signup");
    return c.html(authPage("Sign in", `
        <p class="text-sm text-slate-600 mb-6">Sign in to manage your agents.</p>
        <form method="POST" action="/login" class="space-y-4">
          <div>
            <label class="block text-xs font-medium mb-1 text-slate-700">Email</label>
            <input name="email" type="email" required autofocus class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
          </div>
          <div>
            <label class="block text-xs font-medium mb-1 text-slate-700">Password</label>
            <input name="password" type="password" required class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm">
          </div>
          <button type="submit" class="w-full bg-slate-900 text-white px-4 py-2 rounded-lg font-medium">Sign in</button>
        </form>
    `));
});

app.post("/login", async (c) => {
    const body = await c.req.parseBody();
    const email = String(body.email ?? "").trim().toLowerCase();
    const password = String(body.password ?? "");
    const user = userByEmail(email);
    const ok = user && await verifyPassword(user, password);
    if (!ok || !user) {
        return c.html(authPage("Sign in", `
            <p class="text-rose-600 text-sm mb-3">Wrong email or password.</p>
            <a href="/login" class="underline">Try again</a>`));
    }
    const session = createSession(user.id);
    setCookie(c, SESSION_COOKIE, session, {
        httpOnly: true, sameSite: "Lax", path: "/",
        maxAge: 30 * 86400,
    });
    return c.redirect("/");
});

app.post("/logout", (c) => {
    const session = getCookie(c, SESSION_COOKIE);
    if (session) destroySession(session);
    deleteCookie(c, SESSION_COOKIE, { path: "/" });
    return c.redirect("/login");
});

app.get("/logout", (c) => {
    // Convenience GET so a link works
    const session = getCookie(c, SESSION_COOKIE);
    if (session) destroySession(session);
    deleteCookie(c, SESSION_COOKIE, { path: "/" });
    return c.redirect("/login");
});

// ─── routes ───────────────────────────────────────────────────────────────

app.get("/", (c) => {
    const agents = listAgents();

    // Empty state: wizard-style welcome + "create your first agent" CTA.
    if (agents.length === 0) {
        return c.html(layout("Agents", card(`
            ${pageHeader("Welcome to AgentHQ", "The platform is installed. Now provision your first agent.")}
            <div class="space-y-6">
              <section>
                <h2 class="font-medium mb-2">What's next</h2>
                <ol class="list-decimal list-inside text-slate-700 space-y-1">
                  <li>Create an agent (Linux user + claude install + config)</li>
                  <li>Sign in to your Anthropic account so the agent can call the API</li>
                  <li>Drop in a Telegram bot token so it can talk to you</li>
                  <li>Send a test message — confirm end to end</li>
                </ol>
              </section>
              <div class="pt-2">
                ${button("Create your first agent", { href: "/setup/agent" })}
              </div>
            </div>
        `), "agents", c.get("user")));
    }

    // Dashboard: 1+ agents — persistent control surface.
    const agentRows = agents.map((a) => {
        const dot = a.status === "active" ? "bg-emerald-500" : "bg-slate-300";
        return `
            <a href="/agent/${encodeURIComponent(a.name)}"
               class="flex items-center justify-between p-4 rounded-lg border border-slate-200 hover:border-slate-400 transition">
              <div class="flex items-center gap-3">
                <span class="w-2.5 h-2.5 rounded-full ${dot}"></span>
                <span class="font-mono font-medium">${escapeHtml(a.name)}</span>
              </div>
              <span class="text-sm text-slate-500">${escapeHtml(a.status)}</span>
            </a>`;
    }).join("");

    return c.html(layout("Agents", `
        <div class="flex items-center justify-between mb-6">
          <div>
            <h1 class="text-2xl font-semibold tracking-tight">Agents</h1>
            <p class="text-slate-600 text-sm">${agents.length} agent${agents.length === 1 ? "" : "s"} on this host</p>
          </div>
          ${button("+ Add agent", { href: "/setup/agent" })}
        </div>

        ${card(`
          <div class="space-y-2">${agentRows}</div>
        `)}
    `, "agents", c.get("user")));
});

// Placeholder section pages — sketch the future shape

// Read all available integration manifests from /opt/agents/tools/<id>/tool.json
function listIntegrations(): Array<{
    id: string;
    title: string;
    description: string;
    tools: string[];
    credentialsCount: number;
    active: boolean;
}> {
    const toolsDir = process.env.AGENTHQ_TOOLS_DIR ?? "/opt/agents/tools";
    if (!existsSync(toolsDir)) return [];
    const out: any[] = [];
    for (const id of readdirSync(toolsDir)) {
        if (id.startsWith("_")) continue;  // _format, _example, etc.
        const manifestPath = `${toolsDir}/${id}/tool.json`;
        if (!existsSync(manifestPath)) continue;
        try {
            const m = JSON.parse(readFileSync(manifestPath, "utf8"));
            // Active = at least the first credential exists in the vault
            const firstCred = m.credentials?.[0]?.key;
            const active = firstCred
                ? existsSync(`/etc/agents/credentials/${firstCred}.cred`)
                : true;
            out.push({
                id: m.name ?? id,
                title: m.title ?? id,
                description: m.description ?? "",
                tools: Object.keys(m.tools ?? {}),
                credentialsCount: (m.credentials ?? []).length,
                active,
            });
        } catch {}
    }
    return out.sort((a, b) => a.title.localeCompare(b.title));
}

app.get("/integrations", (c) => {
    const integrations = listIntegrations();
    const cards = integrations.length === 0
        ? `<p class="text-slate-500 italic">No integrations available yet. Tool manifests live under <code>/opt/agents/tools/&lt;name&gt;/tool.json</code>.</p>`
        : integrations.map((it) => `
            <div class="bg-white rounded-xl border border-slate-200 p-5">
              <div class="flex items-start justify-between gap-3 mb-2">
                <div>
                  <h3 class="font-medium text-base">${escapeHtml(it.title)}</h3>
                  <p class="text-xs text-slate-500 font-mono">${escapeHtml(it.id)}</p>
                </div>
                ${it.active
                    ? `<span class="inline-flex items-center gap-1.5 text-xs text-emerald-700 bg-emerald-50 px-2 py-1 rounded"><span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>Active</span>`
                    : `<span class="inline-flex items-center gap-1.5 text-xs text-slate-500 bg-slate-100 px-2 py-1 rounded"><span class="w-1.5 h-1.5 rounded-full bg-slate-400"></span>Inactive</span>`
                }
              </div>
              <p class="text-sm text-slate-600 mb-3">${escapeHtml(it.description)}</p>
              <p class="text-xs text-slate-500 mb-3">${it.tools.length} tool${it.tools.length === 1 ? "" : "s"} · ${it.credentialsCount} credential${it.credentialsCount === 1 ? "" : "s"} required</p>
              <div class="flex gap-2">
                <a href="/integrations/${encodeURIComponent(it.id)}" class="inline-block px-3 py-1.5 rounded-lg font-medium text-sm bg-slate-100 text-slate-900 hover:bg-slate-200">Details</a>
                ${it.active
                    ? `<a href="/integrations/${encodeURIComponent(it.id)}/configure" class="inline-block px-3 py-1.5 rounded-lg font-medium text-sm bg-slate-100 text-slate-900 hover:bg-slate-200">Configure</a>`
                    : `<a href="/integrations/${encodeURIComponent(it.id)}/activate" class="inline-block px-3 py-1.5 rounded-lg font-medium text-sm bg-slate-900 text-white hover:bg-slate-700">Activate</a>`
                }
              </div>
            </div>
        `).join("");

    return c.html(layout("Integrations", `
        <div class="mb-6">
          <h1 class="text-2xl font-semibold tracking-tight">Integrations</h1>
          <p class="text-slate-600 text-sm">External services your agents can connect to. Activate one to make it available across all agents.</p>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">${cards}</div>
        <p class="text-xs text-slate-500 mt-6">
          Adding more integrations: drop a new <code>tool.json</code> manifest into
          <code>/opt/agents/tools/&lt;name&gt;/</code>. The platform discovers it on
          next page load. Schema in <code>tools/_format/README.md</code>.
        </p>
    `, "integrations", c.get("user")));
});

// Per-integration detail page — shows the full manifest + setup.md
app.get("/integrations/:id", (c) => {
    const id = c.req.param("id");
    if (!/^[a-z][a-z0-9_-]*$/.test(id)) return c.html(errorPage("Invalid integration id"));
    const toolsDir = process.env.AGENTHQ_TOOLS_DIR ?? "/opt/agents/tools";
    const manifestPath = `${toolsDir}/${id}/tool.json`;
    if (!existsSync(manifestPath)) return c.html(errorPage(`No such integration: ${id}`));
    let m: any;
    try { m = JSON.parse(readFileSync(manifestPath, "utf8")); } catch { return c.html(errorPage("Manifest invalid")); }

    const setupPath = `${toolsDir}/${id}/setup.md`;
    const setup = existsSync(setupPath) ? readFileSync(setupPath, "utf8") : "";
    const firstCred = m.credentials?.[0]?.key;
    const active = firstCred ? existsSync(`/etc/agents/credentials/${firstCred}.cred`) : true;

    const toolRows = Object.entries<any>(m.tools ?? {}).map(([name, meta]) => `
        <tr class="border-b border-slate-100 last:border-0">
          <td class="py-2 pr-4 font-mono text-sm">${escapeHtml(name)}</td>
          <td class="py-2 text-sm text-slate-600">${escapeHtml(meta.description ?? "")}</td>
          <td class="py-2 text-right">${meta.destructive ? `<span class="text-xs text-rose-600">⚠ destructive</span>` : ""}</td>
        </tr>`).join("");

    const credRows = (m.credentials ?? []).map((c: any) => `
        <tr class="border-b border-slate-100 last:border-0">
          <td class="py-2 pr-4 font-mono text-sm">${escapeHtml(c.key)}</td>
          <td class="py-2 text-sm">${escapeHtml(c.label ?? "")}</td>
          <td class="py-2 text-xs text-slate-500">${c.secret ? "secret" : ""}</td>
        </tr>`).join("");

    return c.html(layout(m.title ?? id, `
        <div class="flex items-baseline justify-between mb-6">
          <div>
            <h1 class="text-2xl font-semibold tracking-tight">${escapeHtml(m.title ?? id)}</h1>
            <p class="text-slate-500 text-sm font-mono">${escapeHtml(id)}</p>
          </div>
          ${active
            ? `<a href="/integrations/${id}/configure" class="inline-block px-4 py-2 rounded-lg font-medium bg-slate-100 text-slate-900 hover:bg-slate-200">Configure</a>`
            : `<a href="/integrations/${id}/activate" class="inline-block px-4 py-2 rounded-lg font-medium bg-slate-900 text-white hover:bg-slate-700">Activate</a>`
          }
        </div>
        ${card(`
          <p class="text-slate-700 mb-4">${escapeHtml(m.description ?? "")}</p>

          <h3 class="font-medium mt-4 mb-2">Tools (${Object.keys(m.tools ?? {}).length})</h3>
          <table class="w-full text-left">
            <thead><tr class="text-xs text-slate-500 uppercase border-b border-slate-200"><th class="pr-4 pb-2">Name</th><th class="pb-2">Description</th><th></th></tr></thead>
            <tbody>${toolRows}</tbody>
          </table>

          <h3 class="font-medium mt-6 mb-2">Required credentials (${(m.credentials ?? []).length})</h3>
          <table class="w-full text-left">
            <thead><tr class="text-xs text-slate-500 uppercase border-b border-slate-200"><th class="pr-4 pb-2">Key</th><th class="pb-2">Label</th><th class="pb-2"></th></tr></thead>
            <tbody>${credRows}</tbody>
          </table>
        `)}
        ${setup ? `
          <div class="mt-6 bg-white rounded-xl border border-slate-200 p-6">
            <h3 class="font-medium mb-3">Setup instructions</h3>
            <pre class="whitespace-pre-wrap text-sm text-slate-700">${escapeHtml(setup)}</pre>
          </div>` : ""}
        <div class="mt-4">${button("Back to integrations", { href: "/integrations", intent: "secondary" })}</div>
    `, "integrations", c.get("user")));
});

// Refresh the systemd credential drop-ins for an agent, split across the
// two-unit (Option A — paired-user) layout:
//
//   agent@<name>.service.d/credentials.conf
//       Loaded as the agent user. Telegram bot token only — agent-prelaunch
//       reads it to write ~/.claude/channels/telegram/.env. Compromised
//       Claude can already misuse this connection regardless, so storing
//       the token under the agent uid doesn't widen the attack surface.
//
//   agent-mcp-creds@<name>.service.d/credentials.conf
//       Loaded as the trusted -mcp peer. Every tool-credential lives here.
//       agent-mcp-creds-install copies them to /run/agents/<name>-mcp/
//       credentials/, which the agent uid cannot read.
//
// We restart agent-mcp-creds@<name>.service after writing so the new creds
// land in /run/agents/<name>-mcp/credentials/ on the next launcher call.
// (Restart is safe: RuntimeDirectoryPreserve=restart keeps the dir around,
// and the launcher reads creds at server-spawn time, not via long-lived fd.)
async function writeSystemdDropIn(agentName: string): Promise<void> {
    const settingsPath = `/home/${agentName}/.claude/settings.json`;
    if (!existsSync(settingsPath)) return;

    let allow: string[] = [];
    try {
        allow = (JSON.parse(readFileSync(settingsPath, "utf8"))?.permissions?.allow ?? []) as string[];
    } catch { return; }

    // Which managed (non-plugin) MCP servers does the agent have any tool granted from?
    const grantedServers = new Set<string>();
    for (const entry of allow) {
        if (!entry.startsWith("mcp__") || entry.startsWith("mcp__plugin_")) continue;
        const m = entry.match(/^mcp__([^_]+(?:_[^_]+)*?)__/);
        if (m) grantedServers.add(m[1]);
    }

    const fs = await import("node:fs");

    // 1. Agent unit drop-in — telegram bot token only.
    const agentLines: string[] = [
        "# Auto-generated by agent-control-web — do not edit by hand.",
        "# Regenerated on every permissions save.",
        "# Telegram bot token only; tool creds live in agent-mcp-creds@*.service.",
        "[Service]",
        `LoadCredentialEncrypted=${agentName}_telegram_bot_token:/etc/agents/credentials/${agentName}_telegram_bot_token.cred`,
    ];
    const agentDropinDir = `/etc/systemd/system/agent@${agentName}.service.d`;
    fs.mkdirSync(agentDropinDir, { recursive: true });
    fs.writeFileSync(`${agentDropinDir}/credentials.conf`, agentLines.join("\n") + "\n");

    // 2. -mcp peer drop-in — every other cred the agent's granted tools need.
    const mcpLines: string[] = [
        "# Auto-generated by agent-control-web — do not edit by hand.",
        "# Regenerated on every permissions save.",
        "[Service]",
    ];
    for (const id of grantedServers) {
        const manifest = loadManifest(id);
        if (!manifest) continue;
        for (const cred of (manifest.credentials ?? [])) {
            const key = cred.key;
            if (!/^[a-zA-Z0-9_-]+$/.test(key)) continue;
            mcpLines.push(`LoadCredentialEncrypted=${key}:/etc/agents/credentials/${key}.cred`);
        }
    }
    const mcpDropinDir = `/etc/systemd/system/agent-mcp-creds@${agentName}.service.d`;
    fs.mkdirSync(mcpDropinDir, { recursive: true });
    fs.writeFileSync(`${mcpDropinDir}/credentials.conf`, mcpLines.join("\n") + "\n");

    spawnSync("systemctl", ["daemon-reload"]);
    // Re-stage decrypted creds into /run/agents/<name>-mcp/credentials/
    // so the next launcher invocation picks up the new set. Stop+start
    // (rather than restart) is needed because oneshot+RemainAfterExit
    // units treat themselves as "still active" and won't re-run ExecStart
    // on `restart` alone.
    spawnSync("systemctl", ["stop",  `agent-mcp-creds@${agentName}.service`]);
    spawnSync("systemctl", ["start", `agent-mcp-creds@${agentName}.service`]);
}

// Generate /home/<agent>/.mcp.json registering every ACTIVE integration as
// an MCP server. Claude reads this at session start to discover available
// servers; settings.json's permissions.allow then gates which specific
// tools the agent can actually call.
//
// Option A — paired-user model: each entry's command is /usr/bin/sudo,
// invoking agent-mcp-launcher under the trusted -mcp peer. The agent
// uid (which is what claude runs as) cannot read the creds itself; only
// the launcher (running as <agent>-mcp) can.
//
// Without this, even a fully-permitted tool fails because claude doesn't
// know the server exists.
async function writeMcpJson(agentName: string): Promise<void> {
    const fs = await import("node:fs");
    const integrations = listIntegrations().filter((it) => it.active);
    const toolsDir = process.env.AGENTHQ_TOOLS_DIR ?? "/opt/agents/tools";
    const mcpServers: Record<string, any> = {};
    for (const it of integrations) {
        const venvPython = `${toolsDir}/${it.id}/.venv/bin/python`;
        const serverPy = `${toolsDir}/${it.id}/server.py`;
        const serverTs = `${toolsDir}/${it.id}/server.ts`;
        // Only emit entries for tools that actually have a runnable
        // entrypoint on disk — otherwise claude discovers a phantom server
        // that fails on first invocation.
        if (!((existsSync(venvPython) && existsSync(serverPy)) || existsSync(serverTs))) continue;
        mcpServers[it.id] = {
            type: "stdio",
            command: "/usr/bin/sudo",
            // -n: never prompt. Failure to authorise must surface as an
            //     immediate sudo error, never as a silent hang waiting on
            //     a password the LLM can't provide.
            args: ["-u", `${agentName}-mcp`, "-n", "/opt/agents/bin/agent-mcp-launcher", it.id],
        };
    }
    const path = `/home/${agentName}/.mcp.json`;
    fs.writeFileSync(path, JSON.stringify({ mcpServers }, null, 2));
    spawnSync("/bin/chown", [`${agentName}:${agentName}`, path]);

    // Also pre-approve each integration in the agent's .claude.json so the
    // first claude session doesn't prompt to trust the .mcp.json servers.
    const claudeJsonPath = `/home/${agentName}/.claude.json`;
    if (existsSync(claudeJsonPath)) {
        try {
            const claudeJson = JSON.parse(fs.readFileSync(claudeJsonPath, "utf8"));
            const homeKey = `/home/${agentName}`;
            claudeJson.projects ??= {};
            claudeJson.projects[homeKey] ??= {};
            claudeJson.projects[homeKey].enabledMcpjsonServers = Object.keys(mcpServers);
            fs.writeFileSync(claudeJsonPath, JSON.stringify(claudeJson, null, 2));
            spawnSync("/bin/chown", [`${agentName}:${agentName}`, claudeJsonPath]);
        } catch {}
    }
}

// Sync /home/<agent>/agent.toml's [tools].enabled and [permissions] blocks
// with the granted set in settings.json.
//
// agent-mcp-launcher gates spawn-or-refuse on agent.toml's [tools].enabled —
// a third allowlist alongside settings.json (claude's permission gate) and
// .mcp.json (claude's discovery gate). Without this sync, granting a tool
// in the UI updates the first two but leaves the launcher refusing to start
// the MCP server, and claude — seeing no server — tells the user it has no
// such tool. The agent ends up apologising for a permission the operator
// just granted.
//
// Preserves [agent] and [telegram] sections (written once by agent-control
// at create time). Rewrites [tools] and [permissions] from settings.json.
async function writeAgentToml(agentName: string): Promise<void> {
    const tomlPath = `/home/${agentName}/agent.toml`;
    const settingsPath = `/home/${agentName}/.claude/settings.json`;
    if (!existsSync(tomlPath) || !existsSync(settingsPath)) return;

    const fs = await import("node:fs");

    let allow: string[] = [];
    try {
        allow = (JSON.parse(fs.readFileSync(settingsPath, "utf8"))?.permissions?.allow ?? []) as string[];
    } catch { return; }

    // server -> set of granted tool names. Skips claude built-ins (Bash, Read,
    // …) and plugin tools (mcp__plugin_*) since neither runs through
    // agent-mcp-launcher and so neither belongs in agent.toml's allowlist.
    const grantedTools = new Map<string, Set<string>>();
    for (const entry of allow) {
        if (!entry.startsWith("mcp__") || entry.startsWith("mcp__plugin_")) continue;
        const m = entry.match(/^mcp__([^_]+(?:_[^_]+)*?)__(.+)$/);
        if (!m) continue;
        const [, server, tool] = m;
        if (!grantedTools.has(server)) grantedTools.set(server, new Set());
        grantedTools.get(server)!.add(tool);
    }

    // [tools].enabled: telegram (always — it's the channel) plus every
    // managed server with at least one granted tool. Sorted for diff stability.
    const enabledTools = new Set<string>(["telegram"]);
    for (const server of grantedTools.keys()) enabledTools.add(server);
    const enabledArr = [...enabledTools].sort();
    const enabledLine = `enabled = [${enabledArr.map((t) => `"${t}"`).join(", ")}]`;

    // [permissions]: per-server, "*" if every tool the manifest exports is
    // granted, else explicit list. The launcher accepts both forms.
    const permLines: string[] = [];
    for (const [server, tools] of [...grantedTools.entries()].sort(([a], [b]) => a.localeCompare(b))) {
        const manifest = loadManifest(server);
        const allTools: string[] = manifest?.tools ? Object.keys(manifest.tools) : [];
        const grantsAll = allTools.length > 0 && allTools.every((t) => tools.has(t));
        if (grantsAll) {
            permLines.push(`${server} = "*"`);
        } else {
            const sortedTools = [...tools].sort();
            permLines.push(`${server} = [${sortedTools.map((t) => `"${t}"`).join(", ")}]`);
        }
    }

    // Preserve everything before [tools] verbatim — that's [agent] + [telegram],
    // owned by agent-control and possibly hand-edited (persona, chat_id).
    const existing = fs.readFileSync(tomlPath, "utf8");
    const toolsIdx = existing.search(/^\[tools\][ \t]*$/m);
    const preamble = (toolsIdx >= 0 ? existing.slice(0, toolsIdx) : existing).trimEnd();

    const out =
        preamble + "\n\n" +
        "[tools]\n" +
        "# Tools this agent is allowed to invoke. Must each exist under /opt/agents/tools/.\n" +
        "# Regenerated by agent-control-web on every permissions save.\n" +
        enabledLine + "\n" +
        "\n" +
        "# Per-tool permissions. Edit via /agent/<name>/permissions in the web UI,\n" +
        "# or in this file directly (then restart agent@<name>.service).\n" +
        "#\n" +
        "# Format per MCP server:\n" +
        "#   server_name = \"*\"             grant ALL of this server's tools\n" +
        "#   server_name = [\"a\", \"b\"]      grant only listed tools\n" +
        "#   server_name not present       deny the whole server\n" +
        "#\n" +
        "# Built-in plugins (telegram, claude-mem) are always granted — they're\n" +
        "# how the agent talks to you and remembers things.\n" +
        "[permissions]\n" +
        (permLines.length === 0 ? "# (none granted)\n" : permLines.join("\n") + "\n");

    const tmp = `${tomlPath}.tmp`;
    fs.writeFileSync(tmp, out, { encoding: "utf8" });
    fs.renameSync(tmp, tomlPath);
    spawnSync("/bin/chown", [`${agentName}:${agentName}`, tomlPath]);
}

// Helper — read manifest for an integration
function loadManifest(id: string): any | null {
    if (!/^[a-z][a-z0-9_-]*$/.test(id)) return null;
    const toolsDir = process.env.AGENTHQ_TOOLS_DIR ?? "/opt/agents/tools";
    const path = `${toolsDir}/${id}/tool.json`;
    if (!existsSync(path)) return null;
    try { return JSON.parse(readFileSync(path, "utf8")); } catch { return null; }
}

// ─── OAuth helper (manifest-driven activation flow) ───────────────────────
//
// When tool.json declares an `oauth` block, the activate page renders an
// inline helper that walks the operator through the authorization-code
// dance. The user enters their API Key + Secret in the form, the helper
// builds the authorize URL on the fly, the user signs in to the upstream
// provider in a new tab, the provider redirects to http://localhost
// (which fails in the browser by design — that 404 page just lets the
// user copy the `?code=…` URL out of the address bar), the user pastes
// that URL back, and the wizard exchanges it server-side for the
// refresh_token. Optional `discovery` hooks then auto-fill follow-up
// fields (e.g. company file GUID) so the operator almost never has to
// type anything beyond the API Key + Secret.
type OAuthManifest = {
    authorize_url: string;
    token_url: string;
    redirect_uri: string;
    scope: string;
    client_id_field: string;
    client_secret_field: string;
    refresh_token_field: string;
    discovery?: string;
};

// Returns the credential key whose value will be auto-filled by the
// discovery step. Tool-specific lookup; add a branch when wiring a new
// OAuth tool that needs auto-discovery (e.g. xero tenant_id).
function discoveryTargetField(oauth: OAuthManifest, m: any): string | undefined {
    if (oauth.discovery === "myob_company_files") {
        const cred = (m.credentials ?? []).find((c: any) => c.key === "myob_business_id");
        return cred ? "myob_business_id" : undefined;
    }
    return undefined;
}

function renderOAuthHelper(id: string, oauth: OAuthManifest): string {
    return `
      <div class="rounded-xl border border-blue-200 bg-blue-50 p-4 space-y-3"
           data-oauth-helper
           data-oauth-id="${escapeHtml(id)}"
           data-client-id-field="cred-${escapeHtml(oauth.client_id_field)}"
           data-client-secret-field="cred-${escapeHtml(oauth.client_secret_field)}"
           data-refresh-token-field="cred-${escapeHtml(oauth.refresh_token_field)}"
           data-authorize-url="${escapeHtml(oauth.authorize_url)}"
           data-redirect-uri="${escapeHtml(oauth.redirect_uri)}"
           data-scope="${escapeHtml(oauth.scope)}"
           data-discovery="${escapeHtml(oauth.discovery ?? "")}">
        <div>
          <h4 class="font-medium text-slate-900">Authorize this integration</h4>
          <p class="text-xs text-slate-600 mt-1" data-oauth-stage-help>Fill in <strong>API Key</strong> and <strong>API Secret</strong> above, then come back here.</p>
        </div>

        <!-- Stage 1 — kick off the OAuth dance ─────────────────────────── -->
        <div data-oauth-stage="1">
          <a href="#" data-oauth-authorize
             class="pointer-events-none opacity-50 block w-full text-center px-4 py-3 rounded-lg font-medium text-sm bg-slate-900 text-white hover:bg-slate-700"
             target="_blank" rel="noopener noreferrer">Sign in with provider →</a>
          <p class="text-xs text-slate-500 mt-2" data-oauth-authorize-hint>(enter API Key first to enable)</p>
        </div>

        <!-- Stage 2 — paste the redirected URL back ────────────────────── -->
        <div data-oauth-stage="2" class="hidden space-y-2">
          <div class="rounded-lg bg-white border border-amber-300 p-3 text-xs text-slate-700 space-y-1">
            <p class="font-medium text-slate-900">After you sign in, the browser will try to load <code class="px-1 rounded bg-slate-100">${escapeHtml(oauth.redirect_uri)}/?code=…</code> and show a "this site can't be reached" error.</p>
            <p>That's expected — nothing is listening at that address. The address bar will still contain the full URL with the code we need.</p>
            <p><strong>Copy the entire URL from the address bar</strong> (click the address bar, then <kbd class="px-1 rounded bg-slate-100 border border-slate-200">Ctrl</kbd>+<kbd class="px-1 rounded bg-slate-100 border border-slate-200">A</kbd>, <kbd class="px-1 rounded bg-slate-100 border border-slate-200">Ctrl</kbd>+<kbd class="px-1 rounded bg-slate-100 border border-slate-200">C</kbd>) and paste it below:</p>
          </div>
          <div class="flex gap-2">
            <input data-oauth-redirect-input type="text" autocomplete="off" spellcheck="false"
                   placeholder="${escapeHtml(oauth.redirect_uri)}/?code=..."
                   class="flex-1 min-w-0 rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs">
            <button type="button" data-oauth-exchange
                    class="shrink-0 px-3 py-2 rounded-lg font-medium text-sm bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50">
              Continue
            </button>
          </div>
        </div>

        <!-- Stage 3 — discovery picker (only shown when >1 result) ──────── -->
        <div data-oauth-stage="3" class="hidden space-y-2">
          <label class="block text-sm font-medium text-slate-900" data-oauth-discovery-label>Choose:</label>
          <select data-oauth-discovery-select required
                  class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white"></select>
        </div>

        <div data-oauth-status class="text-xs hidden"></div>
      </div>
      <script>
      (function () {
        const helper = document.querySelector('[data-oauth-helper][data-oauth-id="${escapeHtml(id)}"]');
        if (!helper) return;
        const id = helper.dataset.oauthId;
        const clientIdInput     = document.getElementById(helper.dataset.clientIdField);
        const clientSecretInput = document.getElementById(helper.dataset.clientSecretField);
        const refreshInput      = document.getElementById(helper.dataset.refreshTokenField);
        const authorizeBaseUrl  = helper.dataset.authorizeUrl;
        const redirectUri       = helper.dataset.redirectUri;
        const scope             = helper.dataset.scope;
        const authorizeBtn      = helper.querySelector('[data-oauth-authorize]');
        const authorizeHint     = helper.querySelector('[data-oauth-authorize-hint]');
        const stageHelp         = helper.querySelector('[data-oauth-stage-help]');
        const stage1            = helper.querySelector('[data-oauth-stage="1"]');
        const stage2            = helper.querySelector('[data-oauth-stage="2"]');
        const stage3            = helper.querySelector('[data-oauth-stage="3"]');
        const redirectInput     = helper.querySelector('[data-oauth-redirect-input]');
        const exchangeBtn       = helper.querySelector('[data-oauth-exchange]');
        const discoverySelect   = helper.querySelector('[data-oauth-discovery-select]');
        const discoveryLabel    = helper.querySelector('[data-oauth-discovery-label]');
        const statusEl          = helper.querySelector('[data-oauth-status]');

        function showStage(n) {
          [stage1, stage2, stage3].forEach((el, i) => {
            if (!el) return;
            if (i + 1 === n) el.classList.remove('hidden');
            else el.classList.add('hidden');
          });
        }

        function buildAuthorizeUrl() {
          const cid = (clientIdInput && clientIdInput.value || '').trim();
          if (!cid) return '';
          const params = new URLSearchParams({
            client_id: cid,
            redirect_uri: redirectUri,
            response_type: 'code',
            scope: scope,
          });
          return authorizeBaseUrl + '?' + params.toString();
        }

        function refreshAuthorizeBtn() {
          const cid = (clientIdInput && clientIdInput.value || '').trim();
          const csec = (clientSecretInput && clientSecretInput.value || '').trim();
          const url = buildAuthorizeUrl();
          if (url && csec) {
            authorizeBtn.href = url;
            authorizeBtn.classList.remove('pointer-events-none', 'opacity-50');
            authorizeHint.textContent = 'Opens the provider in a new tab. Come back here once you see the "site can\\'t be reached" error.';
          } else {
            authorizeBtn.removeAttribute('href');
            authorizeBtn.classList.add('pointer-events-none', 'opacity-50');
            authorizeHint.textContent = url ? '(enter API Secret first to enable)' : '(enter API Key first to enable)';
          }
        }

        function setStatus(kind, text) {
          statusEl.classList.remove('hidden', 'text-slate-600', 'text-red-600', 'text-emerald-700');
          statusEl.classList.add(
            kind === 'error' ? 'text-red-600' :
            kind === 'success' ? 'text-emerald-700' : 'text-slate-600'
          );
          statusEl.textContent = text;
        }

        function clearStatus() { statusEl.classList.add('hidden'); statusEl.textContent = ''; }

        function setHiddenValue(fieldName, value) {
          const el = document.getElementById('cred-' + fieldName);
          if (el) el.value = value;
        }

        if (clientIdInput) clientIdInput.addEventListener('input', refreshAuthorizeBtn);
        if (clientSecretInput) clientSecretInput.addEventListener('input', refreshAuthorizeBtn);
        refreshAuthorizeBtn();

        // Reveal the paste-redirect-URL stage once the user actually clicks
        // the authorize link. Doing it on click means we don't show the
        // confusing paste box before the user even sees the provider screen.
        if (authorizeBtn) authorizeBtn.addEventListener('click', () => {
          if (authorizeBtn.classList.contains('pointer-events-none')) return;
          showStage(2);
          stageHelp.innerHTML = 'After you sign in, copy the URL from the failed-page address bar and paste it below.';
        });

        if (exchangeBtn) exchangeBtn.addEventListener('click', async () => {
          const url = (redirectInput.value || '').trim();
          const cid = (clientIdInput && clientIdInput.value || '').trim();
          const csec = (clientSecretInput && clientSecretInput.value || '').trim();
          if (!url) { setStatus('error', 'Paste the redirect URL first.'); return; }
          if (!cid || !csec) { setStatus('error', 'API Key / Secret above are missing.'); return; }
          exchangeBtn.disabled = true;
          setStatus('info', 'Exchanging code for refresh token…');
          try {
            const r = await fetch('/integrations/' + id + '/oauth-exchange', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ client_id: cid, client_secret: csec, redirect_url: url }),
            });
            const data = await r.json();
            if (!r.ok || !data.refresh_token) {
              setStatus('error', data.error || ('Exchange failed (' + r.status + ')'));
              exchangeBtn.disabled = false;
              return;
            }
            if (refreshInput) refreshInput.value = data.refresh_token;

            const opts = data.discovery_options || [];
            const target = data.discovery_target_field;
            if (target && opts.length === 1) {
              // Exactly one match — silently auto-fill, no UI noise.
              setHiddenValue(target, opts[0].value);
              showStage(0);
              stageHelp.innerHTML = '<span class="text-emerald-700 font-medium">✓ Authorized.</span> Click <strong>Activate</strong> below to save.';
              clearStatus();
            } else if (target && opts.length > 1) {
              // Multiple matches — let the user pick.
              discoveryLabel.textContent = 'Multiple options were discovered — choose one:';
              discoverySelect.innerHTML = '';
              for (const opt of opts) {
                const o = document.createElement('option');
                o.value = opt.value;
                o.textContent = opt.label;
                discoverySelect.appendChild(o);
              }
              setHiddenValue(target, discoverySelect.value);
              discoverySelect.addEventListener('change', () => setHiddenValue(target, discoverySelect.value));
              showStage(3);
              stageHelp.innerHTML = '<span class="text-emerald-700 font-medium">✓ Authorized.</span> Pick the right option, then click <strong>Activate</strong> below.';
              clearStatus();
            } else if (target && opts.length === 0) {
              // Discovery declared but returned nothing — surface the
              // hidden field so the operator can type the value by hand.
              showStage(0);
              const wrapper = document.querySelector('[data-cred="' + target + '"]');
              const input   = document.getElementById('cred-' + target);
              if (wrapper) wrapper.classList.remove('hidden');
              if (input)  { input.type = 'text'; input.required = true; }
              stageHelp.innerHTML = '<span class="text-amber-700 font-medium">Authorized,</span> but auto-discovery returned no options — please fill the remaining field by hand and click Activate.';
              clearStatus();
            } else {
              // Manifest declares no discovery — only the refresh_token
              // was hidden, and it's now populated. Operator just clicks
              // Activate.
              showStage(0);
              stageHelp.innerHTML = '<span class="text-emerald-700 font-medium">✓ Authorized.</span> Click <strong>Activate</strong> below to save.';
              clearStatus();
            }
          } catch (e) {
            setStatus('error', String(e));
            exchangeBtn.disabled = false;
          }
        });
      })();
      </script>
    `;
}

app.get("/integrations/:id/activate", (c) => {
    const id = c.req.param("id");
    const m = loadManifest(id);
    if (!m) return c.html(errorPage("No such integration"));
    const oauth = m.oauth as OAuthManifest | undefined;
    const discoveryTarget = oauth ? discoveryTargetField(oauth, m) : undefined;
    const credFields = (m.credentials ?? []).map((cred: any) => {
        // Refresh-token + discovery-target fields are JS-populated by the
        // OAuth helper, so we render them as hidden inputs the operator
        // never sees. They still post on submit. The discovery-target
        // wrapper carries data-cred so the JS can re-surface it as a
        // visible input if discovery returns zero options.
        const isAutoFilled = !!oauth && (
            cred.key === oauth.refresh_token_field ||
            (!!discoveryTarget && cred.key === discoveryTarget)
        );
        if (isAutoFilled) {
            return `
        <div data-cred="${escapeHtml(cred.key)}" class="hidden">
          <label class="block text-sm font-medium mb-1">${escapeHtml(cred.label ?? cred.key)}</label>
          <input id="cred-${escapeHtml(cred.key)}" name="${escapeHtml(cred.key)}"
                 type="hidden">
        </div>
    `;
        }
        return `
        <div data-cred="${escapeHtml(cred.key)}">
          <label class="block text-sm font-medium mb-1">${escapeHtml(cred.label ?? cred.key)}${cred.secret ? " <span class='text-xs text-slate-400 font-normal'>(secret)</span>" : ""}</label>
          <input id="cred-${escapeHtml(cred.key)}" name="${escapeHtml(cred.key)}" required
                 ${cred.secret ? 'type="password" autocomplete="new-password"' : 'type="text" autocomplete="off"'}
                 spellcheck="false" autocapitalize="off" autocorrect="off"
                 data-1p-ignore data-lpignore="true" data-bwignore="true"
                 class="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm">
          ${cred.description ? `<p class="text-xs text-slate-500 mt-1">${escapeHtml(cred.description)}</p>` : ""}
        </div>
    `;}).join("");
    const toolsDir = process.env.AGENTHQ_TOOLS_DIR ?? "/opt/agents/tools";
    const setupPath = `${toolsDir}/${id}/setup.md`;
    const setupMd = existsSync(setupPath) ? readFileSync(setupPath, "utf8") : "";
    const oauthHelper = oauth ? renderOAuthHelper(id, oauth) : "";

    return c.html(layout(`Activate ${m.title ?? id}`, `
        <div class="mb-6">
          <h1 class="text-2xl font-semibold tracking-tight">Activate ${escapeHtml(m.title ?? id)}</h1>
          <p class="text-slate-600 text-sm">Follow the setup steps on the left, paste the resulting credentials on the right.</p>
        </div>
        <div class="grid grid-cols-1 lg:grid-cols-5 gap-6">
          ${setupMd ? `
            <div class="lg:col-span-3 bg-white rounded-xl border border-slate-200 p-6">
              <h3 class="font-medium mb-3">Setup instructions</h3>
              <pre class="whitespace-pre-wrap text-sm text-slate-700 leading-relaxed">${escapeHtml(setupMd)}</pre>
            </div>` : ""}
          <div class="${setupMd ? "lg:col-span-2" : "lg:col-span-5"} bg-white rounded-xl border border-slate-200 p-6 self-start sticky top-4">
            <h3 class="font-medium mb-3">Credentials</h3>
            <p class="text-xs text-slate-500 mb-4">Encrypted into the systemd-creds vault. Never logged or echoed.</p>
            <form method="POST" action="/integrations/${id}/activate" class="space-y-4">
              ${credFields}
              ${oauthHelper}
              <div class="pt-2 flex gap-2">
                ${button(`Activate`)}
                ${button("Cancel", { href: `/integrations/${id}`, intent: "secondary" })}
              </div>
            </form>
          </div>
        </div>
    `, "integrations", c.get("user")));
});

// POST /integrations/:id/oauth-exchange
//
// Called by the OAuth helper JS in the activate page. Body shape:
//   { client_id, client_secret, redirect_url }
// where redirect_url is the FULL pasted URL the upstream provider
// redirected the user to (e.g. http://localhost/?code=ABC...). The
// handler parses the `code` out of the URL, POSTs to oauth.token_url
// with grant_type=authorization_code, returns the refresh_token, and —
// if the manifest declares a discovery hook — runs that hook with the
// freshly-acquired access_token to populate dependent fields.
app.post("/integrations/:id/oauth-exchange", async (c) => {
    const id = c.req.param("id");
    const m = loadManifest(id);
    if (!m || !m.oauth) {
        return c.json({ error: "Integration has no OAuth configuration" }, 400);
    }
    const oauth = m.oauth as OAuthManifest;

    let body: { client_id?: string; client_secret?: string; redirect_url?: string };
    try { body = await c.req.json(); } catch { return c.json({ error: "Invalid JSON body" }, 400); }
    const clientId = (body.client_id ?? "").trim();
    const clientSecret = (body.client_secret ?? "").trim();
    const redirectUrl = (body.redirect_url ?? "").trim();
    if (!clientId || !clientSecret || !redirectUrl) {
        return c.json({ error: "client_id, client_secret, and redirect_url are required" }, 400);
    }

    // Pull `code` out of the pasted URL. If the user happened to copy
    // just the bare code instead of the whole URL, accept that too —
    // URL parsing rejects bare strings, so sniff for a leading scheme.
    let code: string | null = null;
    if (/^https?:\/\//i.test(redirectUrl)) {
        try { code = new URL(redirectUrl).searchParams.get("code"); }
        catch { return c.json({ error: "Could not parse the URL you pasted." }, 400); }
    } else if (redirectUrl.length > 4 && !redirectUrl.includes(" ")) {
        code = redirectUrl;
    }
    if (!code) {
        return c.json({ error: "No `code` parameter found in the pasted URL. Make sure you copied the full URL the browser ended on after sign-in." }, 400);
    }

    // Exchange code → tokens. Form-encoded per OAuth2 spec.
    const tokenForm = new URLSearchParams({
        client_id: clientId,
        client_secret: clientSecret,
        redirect_uri: oauth.redirect_uri,
        code,
        grant_type: "authorization_code",
    });
    let tokenResp: Response;
    try {
        tokenResp = await fetch(oauth.token_url, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: tokenForm.toString(),
        });
    } catch (e) {
        return c.json({ error: `Could not reach ${oauth.token_url}: ${String(e)}` }, 502);
    }
    const tokenText = await tokenResp.text();
    if (!tokenResp.ok) {
        return c.json({ error: `Token exchange failed (${tokenResp.status}): ${tokenText.slice(0, 400)}` }, 400);
    }
    let tokenData: any;
    try { tokenData = JSON.parse(tokenText); }
    catch { return c.json({ error: `Token endpoint returned non-JSON: ${tokenText.slice(0, 200)}` }, 502); }
    const refreshToken = tokenData.refresh_token;
    const accessToken = tokenData.access_token;
    if (!refreshToken) {
        return c.json({ error: `Token response did not include a refresh_token. Make sure the requested scope includes offline_access.` }, 400);
    }

    // Optional discovery hook — runs with the access token we just got.
    // Best-effort: a discovery failure leaves the field as a plain text
    // input so the user can still type the value by hand.
    let discoveryOptions: { value: string; label: string }[] | undefined;
    let discoveryTarget: string | undefined;
    if (oauth.discovery === "myob_company_files" && accessToken) {
        try {
            const r = await fetch("https://api.myob.com/accountright/", {
                headers: {
                    "Authorization": `Bearer ${accessToken}`,
                    "x-myobapi-key": clientId,
                    "x-myobapi-version": "v2",
                    "Accept": "application/json",
                },
            });
            if (r.ok) {
                const files: any = await r.json();
                // MYOB returns a top-level array of CompanyFile records, each
                // with Uri ending in /<GUID>. Pull the GUID for the value;
                // build a friendly label from Name + Country.
                const arr: any[] = Array.isArray(files) ? files : (files?.Items ?? []);
                discoveryOptions = arr.map((f: any) => {
                    const uri: string = f.Uri ?? "";
                    const guid = uri.split("/").filter(Boolean).pop() ?? "";
                    const country = f.Country ? ` — ${f.Country}` : "";
                    return { value: guid, label: `${f.Name ?? guid}${country}` };
                }).filter((o) => o.value);
                discoveryTarget = "myob_business_id";
            }
        } catch {
            // Best-effort: leave the field as plain text on failure.
        }
    }

    return c.json({
        refresh_token: refreshToken,
        discovery_options: discoveryOptions,
        discovery_target_field: discoveryTarget,
    });
});

app.post("/integrations/:id/activate", async (c) => {
    const id = c.req.param("id");
    const m = loadManifest(id);
    if (!m) return c.html(errorPage("No such integration"));

    const body = await c.req.parseBody();
    const errors: string[] = [];

    for (const cred of (m.credentials ?? [])) {
        const value = String(body[cred.key] ?? "").trim();
        if (!value) {
            errors.push(`Missing value for ${cred.key}`);
            continue;
        }
        const r = spawnSync("/usr/local/bin/agenthq-cred", ["set", cred.key], {
            input: value,
            encoding: "utf8",
        });
        if (r.status !== 0) {
            errors.push(`Failed to store ${cred.key}: ${r.stderr || r.stdout}`);
        }
    }

    if (errors.length > 0) {
        return c.html(errorPage(`Activation failed for ${id}`, errors.join("; ")));
    }
    return c.redirect(`/integrations/${id}`);
});

app.get("/integrations/:id/configure", (c) => {
    const id = c.req.param("id");
    const m = loadManifest(id);
    if (!m) return c.html(errorPage("No such integration"));

    const credList = (m.credentials ?? []).map((cred: any) => {
        const stored = existsSync(`/etc/agents/credentials/${cred.key}.cred`);
        return `<li class="flex items-center justify-between py-1.5 border-b border-slate-100 last:border-0">
          <span class="font-mono text-sm">${escapeHtml(cred.key)}</span>
          ${stored
            ? `<span class="text-xs text-emerald-700">stored ✓</span>`
            : `<span class="text-xs text-rose-600">missing</span>`}
        </li>`;
    }).join("");

    return c.html(layout(`Configure ${m.title ?? id}`, card(`
        ${pageHeader(`Configure ${escapeHtml(m.title ?? id)}`)}
        <p class="text-sm text-slate-700 mb-3">Stored credentials (values not shown — they're encrypted in the vault):</p>
        <ul class="mb-6">${credList}</ul>
        <div class="flex gap-3">
          <a href="/integrations/${id}/activate" class="inline-block px-4 py-2 rounded-lg font-medium bg-slate-100 text-slate-900 hover:bg-slate-200">Re-enter credentials</a>
          ${button("Back", { href: `/integrations/${id}`, intent: "secondary" })}
        </div>
    `), "integrations", c.get("user")));
});

app.get("/updates", (c) => c.html(layout("Updates", card(`
    ${pageHeader("Updates", "Keep the platform and the claude binary current.")}
    <p class="text-slate-600">AgentHQ checks for new commits on <code>main</code> and new claude versions from Anthropic.</p>
    <p class="mt-3 text-sm text-slate-500"><em>Coming soon.</em> One-click "Update now" + a nightly background timer that pulls fresh code, runs <code>install.sh</code> idempotently, and gracefully restarts services.</p>
`), "updates", c.get("user"))));

app.get("/settings", (c) => c.html(layout("Settings", card(`
    ${pageHeader("Settings", "Host-level configuration.")}
    <p class="text-slate-600">Telegram defaults, log level, backup path, host nickname, vault method (TPM2/host-key)…</p>
    <p class="mt-3 text-sm text-slate-500"><em>Coming soon.</em></p>
`), "settings", c.get("user"))));

app.get("/setup/agent", (c) => {
    return c.html(layout("Add agent", card(`
        ${pageHeader("Create an agent", "Linux user, claude install, telegram bot wiring — all in one.")}
        <form method="POST" action="/setup/agent" class="space-y-5">
          <div>
            <label class="block text-sm font-medium mb-1">Agent name</label>
            <input name="name" required pattern="[a-z][a-z0-9_-]{1,30}" placeholder="testbot"
              class="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm">
            <p class="text-xs text-slate-500 mt-1">Lowercase letters, digits, hyphens. Starts with a letter.</p>
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">Persona</label>
            <textarea name="persona" rows="2" placeholder="A friendly executive assistant..."
              class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"></textarea>
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">Your Telegram chat ID</label>
            <input name="telegram_chat_id" required pattern="[0-9]+" placeholder="123456789"
              class="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm">
            <p class="text-xs text-slate-500 mt-1">Find yours by messaging @userinfobot in Telegram.</p>
          </div>
          <div class="pt-2 flex gap-3">
            ${button("Continue")}
            ${button("Cancel", { href: "/", intent: "secondary" })}
          </div>
        </form>
    `), null, c.get("user")));
});

app.post("/setup/agent", async (c) => {
    const body = await c.req.parseBody();
    const name = String(body.name ?? "").trim();
    const persona = String(body.persona ?? "").trim();
    const chatId = String(body.telegram_chat_id ?? "").trim();

    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) {
        return c.html(errorPage("Invalid agent name", "Must start with a lowercase letter, 2–31 chars from [a-z0-9_-]."));
    }
    if (!/^\d+$/.test(chatId)) {
        return c.html(errorPage("Invalid Telegram chat ID", "Must be all digits."));
    }

    // Single SSE connection on the parent div. Children use sse-swap to
    // pick which event they consume — keeps it to one EventSource (and
    // therefore one agent-control invocation). sse-close="done" stops
    // the auto-reconnect loop once provisioning finishes either way.
    const streamUrl = `/setup/agent/stream?name=${encodeURIComponent(name)}&persona=${encodeURIComponent(persona)}&chat_id=${encodeURIComponent(chatId)}`;
    return c.html(layout("Provisioning", card(`
        ${pageHeader(`Provisioning ${escapeHtml(name)}`, "This takes about a minute. Live output below.")}
        <div hx-ext="sse" sse-connect="${streamUrl}" sse-close="done">
          <pre id="provision-log" sse-swap="line" hx-swap="beforeend"
               class="bg-slate-900 text-slate-100 rounded-lg p-4 text-xs font-mono overflow-auto max-h-96"></pre>
          <div id="next-step" sse-swap="redirect" hx-swap="innerHTML"
               class="mt-4 text-sm text-slate-600">
            When provisioning completes, the next step is Claude OAuth login.
          </div>
        </div>
    `), "agents", c.get("user")));
});

// SSE stream for agent-control output. Emits "line" events for each chunk
// of stdout/stderr, a "redirect" event with HTML to swap into the next-step
// div on success, and a final "done" event so the client closes the
// connection (otherwise EventSource auto-reconnects and re-fires the create).
app.get("/setup/agent/stream", (c) => {
    const name = c.req.query("name") ?? "";
    const persona = c.req.query("persona") ?? "";
    const chatId = c.req.query("chat_id") ?? "";
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.text("invalid name", 400);

    return streamSSE(c, async (s) => {
        await s.writeSSE({ event: "line", data: `[wizard] starting agent-control create ${name}\n` });

        // Server runs as root via systemd. AGENTHQ_SKIP_CLAUDE_LOGIN tells
        // agent-control to defer the OAuth step — the wizard handles that on
        // its own page since OAuth is interactive.
        const child = spawn("/usr/local/bin/agent-control", [
            "create",
            name,
            "--tools", "telegram",
            "--persona", persona,
            "--telegram-chat-id", chatId,
        ], {
            env: { ...process.env, AGENTHQ_SKIP_CLAUDE_LOGIN: "1" },
        });

        // Heartbeat every 5s to keep proxies/browsers from reaping the
        // connection during slow steps (e.g. the 30s claude binary install).
        // Without this, EventSource auto-reconnect re-fires the whole spawn.
        const heartbeat = setInterval(() => {
            s.writeSSE({ event: "ping", data: "" }).catch(() => {});
        }, 5000);

        try {
            for await (const chunk of child.stdout) await s.writeSSE({ event: "line", data: chunk.toString() });
            for await (const chunk of child.stderr) await s.writeSSE({ event: "line", data: chunk.toString() });
        } finally {
            clearInterval(heartbeat);
        }
        const exitCode: number = await new Promise((r) => child.on("close", r));

        await s.writeSSE({ event: "line", data: `\n[wizard] agent-control exited with code ${exitCode}\n` });

        if (exitCode === 0) {
            await s.writeSSE({ event: "line", data: `[wizard] success — taking you to Claude login\n` });
            await s.writeSSE({
                event: "redirect",
                data: `<a href="/setup/claude/${name}" class="underline">Provisioning complete — continue to Claude login →</a><script>setTimeout(()=>location.href="/setup/claude/${name}", 800)</script>`,
            });
        } else {
            await s.writeSSE({ event: "line", data: `[wizard] failed — fix the error above and try again\n` });
        }

        await s.writeSSE({ event: "done", data: "" });
    });
});

// Placeholder pages — the next milestones.
app.get("/setup/claude/:name", (c) => {
    const name = c.req.param("name");
    const ok = claudeAuthenticated(name);
    return c.html(layout("Claude login", card(`
        ${pageHeader("Authenticate Claude", `Agent <code>${escapeHtml(name)}</code> needs to sign in to your Anthropic account.`)}
        ${ok
            ? `<p class="text-emerald-600 font-medium">✅ Already signed in.</p>
               <div class="mt-4">${button("Continue", { href: `/setup/token/${name}` })}</div>`
            : `<ol class="list-decimal list-inside text-sm text-slate-700 space-y-2 mb-4">
                 <li>Open a terminal on this box and run these two commands:
                   ${code(`xhost +SI:localuser:${name}\nsudo -i -u ${name} claude`)}
                   <p class="text-xs text-slate-500 mt-1">First line grants <code>${escapeHtml(name)}</code> access to your X clipboard so claude's "c to copy" works. Second drops you into claude as that user.</p>
                 </li>
                 <li>Inside claude, type <code class="bg-slate-100 px-1 rounded">/login</code> and pick "Claude.ai login"</li>
                 <li>Claude prints a URL. <strong>Press <code class="bg-slate-100 px-1 rounded">c</code> to copy it</strong> (the URL wraps over several lines but the <code>c</code> shortcut grabs it cleanly)</li>
                 <li>Open a browser, paste the URL, sign in to your Anthropic account</li>
                 <li>Browser redirects you with a code — copy it, paste into the "Paste code here if prompted" field in the terminal, hit Enter</li>
                 <li>Type <code class="bg-slate-100 px-1 rounded">/exit</code> to leave claude</li>
               </ol>
               <p class="text-sm text-slate-500">This page auto-refreshes every few seconds…</p>
               <script>setTimeout(() => location.reload(), 4000)</script>`}
    `), null, c.get("user")));
});

app.get("/setup/token/:name", (c) => {
    const name = c.req.param("name");
    return c.html(layout("Telegram bot token", card(`
        ${pageHeader("Drop in your bot token", `So <code>${escapeHtml(name)}</code> can read and reply on Telegram.`)}
        <ol class="list-decimal list-inside text-sm text-slate-700 space-y-1 mb-4">
          <li>Open Telegram, message <a class="underline" href="https://t.me/BotFather">@BotFather</a></li>
          <li>Send <code>/newbot</code>, follow prompts, copy the token it gives you</li>
        </ol>
        <form method="POST" action="/setup/token/${name}" class="space-y-4">
          <div>
            <label class="block text-sm font-medium mb-1">Bot token</label>
            <input name="token" required type="password" autocomplete="off" placeholder="1234567890:AAH..."
              class="w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm">
            <p class="text-xs text-slate-500 mt-1">Stored encrypted in the systemd-creds vault. Never logged.</p>
          </div>
          <div class="flex gap-3 pt-2">
            ${button("Save and start agent")}
            ${button("Cancel", { href: "/", intent: "secondary" })}
          </div>
        </form>
    `), null, c.get("user")));
});

app.post("/setup/token/:name", async (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.html(errorPage("Invalid agent name"));
    const body = await c.req.parseBody();
    const token = String(body.token ?? "").trim();
    if (token.length < 10) return c.html(errorPage("Bot token looks wrong", "Should be in the form 12345:abcdef..."));

    // Store via agenthq-cred set, then start service. (Server runs as root.)
    const credName = `${name}_telegram_bot_token`;
    const r1 = spawnSync("/usr/local/bin/agenthq-cred", ["set", credName], { input: token, encoding: "utf8" });
    if (r1.status !== 0) return c.html(errorPage("Failed to store credential", r1.stderr || ""));

    // Restart (not start) so a service already running with the previous
    // credential value picks up the new one. agent-control's create flow may
    // have started the service eagerly if a stale cred was already in the vault.
    const r2 = spawnSync("systemctl", ["restart", `agent@${name}.service`], { encoding: "utf8" });
    if (r2.status !== 0) return c.html(errorPage("Service failed to start", r2.stderr || ""));

    return c.redirect(`/agent/${name}`);
});

app.get("/agent/:name", (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.html(errorPage("Invalid agent name"));
    const r = spawnSync("systemctl", ["status", `agent@${name}.service`, "--no-pager"], { encoding: "utf8" });
    return c.html(layout(name, `
        <div class="flex items-center justify-between mb-6">
          <div>
            <h1 class="text-2xl font-semibold tracking-tight">${escapeHtml(name)}</h1>
            <p class="text-slate-600 text-sm font-mono">agent@${escapeHtml(name)}.service</p>
          </div>
          <div class="flex gap-2">
            ${button("Permissions", { href: `/agent/${name}/permissions`, intent: "secondary" })}
            ${button("Refresh", { href: `/agent/${name}`, intent: "secondary" })}
            <a href="/agent/${name}/delete" class="inline-block px-4 py-2 rounded-lg font-medium bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200">Delete</a>
          </div>
        </div>
        ${card(`
          ${code(r.stdout || r.stderr)}
          <p class="mt-4 text-sm text-slate-600">If status is <code>active (running)</code>, message your bot in Telegram. It should reply.</p>
        `)}
    `, "agents", c.get("user")));
});

// Delete confirmation page — never delete on a GET. Form posts to /delete
// which actually removes the agent + purges its home and credentials.
app.get("/agent/:name/delete", (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.html(errorPage("Invalid agent name"));
    return c.html(layout(`Delete ${name}`, card(`
        ${pageHeader(`Delete agent ${escapeHtml(name)}?`, "This action cannot be undone.")}
        <p class="text-sm text-slate-700 mb-2">The following will be removed:</p>
        <ul class="list-disc list-inside text-sm text-slate-600 space-y-1 mb-6">
          <li>Linux user <code>${escapeHtml(name)}</code> and the home directory <code>/home/${escapeHtml(name)}</code></li>
          <li>The agent's claude install, memory, conv_log — everything under that home</li>
          <li>The systemd unit drop-in <code>/etc/systemd/system/agent@${escapeHtml(name)}.service.d</code></li>
          <li>Per-agent credentials in <code>/etc/agents/credentials/${escapeHtml(name)}_*.cred</code></li>
        </ul>
        <p class="text-sm text-slate-600 mb-4">Shared credentials (m365, ha, hikvision, etc) are kept — those belong to the platform.</p>
        <form method="POST" action="/agent/${name}/delete" class="flex gap-3">
          <button type="submit" class="px-4 py-2 rounded-lg font-medium bg-rose-600 text-white hover:bg-rose-700">Yes, delete ${escapeHtml(name)}</button>
          ${button("Cancel", { href: `/agent/${name}`, intent: "secondary" })}
        </form>
    `), "agents", c.get("user")));
});

app.post("/agent/:name/delete", (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.html(errorPage("Invalid agent name"));
    const r = spawnSync("/usr/local/bin/agent-control", ["delete", name, "--purge"], { encoding: "utf8" });
    if (r.status !== 0) {
        return c.html(errorPage(`Failed to delete ${name}`, r.stderr || r.stdout || ""));
    }
    return c.redirect("/");
});

// Editable per-agent permissions matrix.
//
// Lists every MCP server that's been ACTIVATED at the platform level
// (i.e. has tool.json on disk + first credential in the vault) and shows
// each tool as a checkbox. Submit POSTs the granted set; backend rewrites
// the agent's settings.json permissions.allow and restarts the service.
//
// Built-in claude tools (Bash/Read/Write/etc) and plugin tools (telegram,
// claude-mem) are always granted — they're how the agent talks and thinks.
app.get("/agent/:name/permissions", (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.html(errorPage("Invalid agent name"));

    const settingsPath = `/home/${name}/.claude/settings.json`;
    let allow: string[] = [];
    try {
        const raw = JSON.parse(readFileSync(settingsPath, "utf8"));
        allow = (raw?.permissions?.allow ?? []) as string[];
    } catch {
        return c.html(errorPage(`No settings.json for ${name}`, "Agent may not exist or wasn't provisioned by AgentHQ."));
    }

    // Currently-granted MCP tools, by server
    const granted: Record<string, Set<string>> = {};
    for (const entry of allow) {
        if (!entry.startsWith("mcp__") || entry.startsWith("mcp__plugin_")) continue;
        const m = entry.match(/^mcp__([^_]+(?:_[^_]+)*?)__(.+)$/);
        if (!m) continue;
        const [, server, tool] = m;
        granted[server] ??= new Set();
        granted[server].add(tool);
    }

    // All ACTIVATED integrations (manifest + first cred in vault)
    const integrations = listIntegrations().filter((it) => it.active);

    const sections = integrations.length === 0
        ? `<div class="bg-white rounded-xl border border-slate-200 p-6 text-sm text-slate-600">
             No integrations activated yet. Visit <a href="/integrations" class="underline">Integrations</a>, activate one (M365, etc), then come back to grant tools to this agent.
           </div>`
        : integrations.map((it) => {
            const m = loadManifest(it.id);
            const tools = (m?.tools ?? {}) as Record<string, any>;
            const grants = granted[it.id] ?? new Set();
            const checkboxes = Object.entries(tools).map(([toolName, meta]) => {
                const checked = grants.has(toolName) ? "checked" : "";
                const destructive = meta.destructive ? `<span class="ml-2 text-xs text-rose-600">⚠ destructive</span>` : "";
                return `
                    <label class="flex items-start gap-3 py-2 border-b border-slate-100 last:border-0 cursor-pointer hover:bg-slate-50 -mx-2 px-2 rounded">
                      <input type="checkbox" name="grant" value="${escapeHtml(it.id)}__${escapeHtml(toolName)}" ${checked}
                             class="mt-1 w-4 h-4 rounded border-slate-300 text-slate-900 focus:ring-slate-400">
                      <div class="flex-1">
                        <div class="flex items-center justify-between">
                          <span class="font-mono text-sm font-medium">${escapeHtml(toolName)}</span>
                          ${destructive}
                        </div>
                        <p class="text-xs text-slate-500">${escapeHtml(meta.description ?? "")}</p>
                      </div>
                    </label>`;
            }).join("");
            return `
                <div class="bg-white rounded-xl border border-slate-200 p-5 mb-4">
                  <div class="flex items-baseline justify-between mb-3">
                    <div>
                      <h3 class="font-medium">${escapeHtml(it.title)}</h3>
                      <p class="text-xs text-slate-500 font-mono">${escapeHtml(it.id)}</p>
                    </div>
                    <div class="flex gap-2">
                      <button type="button" onclick="this.closest('.bg-white').querySelectorAll('input[type=checkbox]').forEach(c => c.checked = true)" class="text-xs text-slate-600 hover:text-slate-900">All</button>
                      <button type="button" onclick="this.closest('.bg-white').querySelectorAll('input[type=checkbox]').forEach(c => c.checked = false)" class="text-xs text-slate-600 hover:text-slate-900">None</button>
                    </div>
                  </div>
                  <div>${checkboxes}</div>
                </div>`;
        }).join("");

    const saved = c.req.query("saved") === "1";

    return c.html(layout(`${name} — permissions`, `
        <div class="flex items-center justify-between mb-6">
          <div>
            <h1 class="text-2xl font-semibold tracking-tight">${escapeHtml(name)} permissions</h1>
            <p class="text-slate-600 text-sm">Tick the tools this agent is allowed to use. Saving restarts the agent so changes take effect.</p>
          </div>
          ${button("Back to agent", { href: `/agent/${name}`, intent: "secondary" })}
        </div>

        ${saved ? `
          <div class="mb-4 px-4 py-3 rounded-lg bg-emerald-50 border border-emerald-200 text-sm text-emerald-800 flex items-center gap-2">
            <span class="font-medium">✓ Saved.</span> ${escapeHtml(name)} restarted and is now using the new permission set.
          </div>` : ""}

        <form method="POST" action="/agent/${name}/permissions">
          ${sections}
          ${integrations.length > 0 ? `
            <div class="flex gap-3 mt-6">
              <button type="submit"
                      onclick="this.disabled=true; this.textContent='Saving and restarting…'; this.form.submit()"
                      class="inline-block px-4 py-2 rounded-lg font-medium bg-slate-900 text-white hover:bg-slate-700 disabled:opacity-60 disabled:cursor-wait">
                Save and restart agent
              </button>
              ${button("Cancel", { href: `/agent/${name}`, intent: "secondary" })}
            </div>
          ` : ""}
        </form>

        <details class="mt-8 text-sm">
          <summary class="text-slate-600 cursor-pointer">Always-granted tools (built-ins + plugins)</summary>
          <div class="mt-3 text-xs text-slate-500">
            <p class="mb-2">Every agent gets the following without configuration:</p>
            <p class="mb-1"><strong>Built-ins:</strong> Bash, Edit, Write, Glob, Grep, Read, WebSearch, WebFetch</p>
            <p><strong>Plugins:</strong> telegram (reply, react, edit_message, download_attachment), claude-mem</p>
          </div>
        </details>
    `, "agents", c.get("user")));
});

// M365 device-flow OAuth — interactive sign-in driven from the web UI.
// Server spawns auth.py as the agent user, parses its JSON events, and
// streams them via SSE to the browser.
app.get("/agent/:name/auth/m365", (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.html(errorPage("Invalid agent name"));

    if (!existsSync("/etc/agents/credentials/m365_client_id.cred")) {
        return c.html(errorPage("M365 not activated", `Activate the M365 integration first at <a href="/integrations/m365">Integrations → M365</a>.`));
    }

    const tokenCache = `/home/${name}/.m365_token_cache.json`;
    if (existsSync(tokenCache)) {
        return c.html(layout(`Authorize ${name} for M365`, card(`
            ${pageHeader(`${escapeHtml(name)} is already signed in to Microsoft 365`, "Re-authorize only if the token has been revoked or you want to switch accounts.")}
            <p class="text-sm text-emerald-700 mb-4">✓ Token cache present at <code>${escapeHtml(tokenCache)}</code></p>
            <div class="flex gap-3">
              ${button("Re-authorize", { href: `/agent/${name}/auth/m365?force=1`, intent: "secondary" })}
              ${button("Back", { href: `/agent/${name}/permissions`, intent: "secondary" })}
            </div>
        `), "agents", c.get("user")));
    }

    return c.html(layout(`Authorize ${name} for M365`, `
        <div class="mb-6">
          <h1 class="text-2xl font-semibold tracking-tight">Authorize ${escapeHtml(name)} for Microsoft 365</h1>
          <p class="text-slate-600 text-sm">One-time device-flow sign-in. ${escapeHtml(name)} will get its own refresh token; nothing shared with other agents.</p>
        </div>
        <div hx-ext="sse" sse-connect="/agent/${name}/auth/m365/stream" sse-close="done"
             class="space-y-4">
          <div id="auth-status" class="bg-white rounded-xl border border-slate-200 p-6 text-sm text-slate-600"
               sse-swap="flow_started,success,error" hx-swap="innerHTML">
            <p>Starting sign-in flow… (one moment)</p>
          </div>
        </div>
    `, "agents", c.get("user")));
});

app.get("/agent/:name/auth/m365/stream", (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.text("invalid", 400);

    return streamSSE(c, async (s) => {
        const venvPython = "/opt/agents/tools/m365/.venv/bin/python";
        const authScript = "/opt/agents/tools/m365/auth.py";

        const child = spawn("sudo", ["-u", name, venvPython, authScript, "--json-flow"], {
            env: { ...process.env, HOME: `/home/${name}` },
        });

        // Heartbeat
        const heartbeat = setInterval(() => {
            s.writeSSE({ event: "ping", data: "" }).catch(() => {});
        }, 5000);

        let buffer = "";
        const handleLine = async (line: string) => {
            line = line.trim();
            if (!line) return;
            try {
                const evt = JSON.parse(line);
                if (evt.event === "flow_started") {
                    const html = `
                        <h3 class="font-medium mb-3">Sign in to authorize ${escapeHtml(name)}</h3>
                        <ol class="list-decimal list-inside space-y-2 text-slate-700 mb-4">
                          <li>Open <a href="${escapeHtml(evt.verification_uri)}" target="_blank" rel="noopener" class="underline font-medium">${escapeHtml(evt.verification_uri)}</a> in a new tab</li>
                          <li>Enter this code: <span class="ml-2 inline-block px-3 py-1.5 rounded font-mono text-lg bg-slate-900 text-white">${escapeHtml(evt.user_code)}</span></li>
                          <li>Sign in to your Microsoft account</li>
                          <li>Come back here — this page updates automatically</li>
                        </ol>
                        <p class="text-xs text-slate-500">Code expires in ${Math.floor((evt.expires_in ?? 0) / 60)} minutes.</p>
                    `;
                    await s.writeSSE({ event: "flow_started", data: html });
                } else if (evt.event === "success") {
                    const html = `
                        <p class="text-emerald-700 font-medium mb-3">✓ ${escapeHtml(name)} authorized as ${escapeHtml(evt.user ?? "unknown")}</p>
                        <p class="text-sm text-slate-600 mb-4">Token cache stored. Sign-in won't be needed again unless the token is revoked.</p>
                        <div class="flex gap-2">
                          <a href="/agent/${name}/permissions" class="inline-block px-4 py-2 rounded-lg font-medium bg-slate-900 text-white">Back to permissions</a>
                        </div>
                    `;
                    await s.writeSSE({ event: "success", data: html });
                } else if (evt.event === "error") {
                    const html = `<p class="text-rose-700 font-medium">Sign-in failed: ${escapeHtml(evt.error ?? "")}</p>
                        <a href="/agent/${name}/auth/m365" class="text-sm underline mt-2 inline-block">Try again</a>`;
                    await s.writeSSE({ event: "error", data: html });
                }
            } catch {
                // Non-JSON line, ignore
            }
        };

        try {
            for await (const chunk of child.stdout) {
                buffer += chunk.toString();
                const lines = buffer.split("\n");
                buffer = lines.pop() ?? "";
                for (const line of lines) await handleLine(line);
            }
            // Flush any trailing line
            if (buffer) await handleLine(buffer);
            // Drain stderr too (errors go there)
            for await (const chunk of child.stderr) {
                const text = chunk.toString();
                console.error(`[m365 auth ${name}]`, text);
            }
        } finally {
            clearInterval(heartbeat);
        }

        await new Promise((r) => child.on("close", r));
        await s.writeSSE({ event: "done", data: "" });
    });
});

app.post("/agent/:name/permissions", async (c) => {
    const name = c.req.param("name");
    if (!/^[a-z][a-z0-9_-]{1,30}$/.test(name)) return c.html(errorPage("Invalid agent name"));

    const settingsPath = `/home/${name}/.claude/settings.json`;
    let settings: any;
    try {
        settings = JSON.parse(readFileSync(settingsPath, "utf8"));
    } catch {
        return c.html(errorPage("Agent settings.json missing"));
    }

    const body = await c.req.parseBody({ all: true });
    const raw = body["grant"];
    const grants: string[] = Array.isArray(raw) ? raw.map(String) : raw ? [String(raw)] : [];

    // Existing allow list, minus any mcp__<integration>__<tool> entries we'll
    // re-derive from the form. Keep built-ins and plugin entries untouched.
    const integrations = listIntegrations().filter((it) => it.active);
    const managedIds = new Set(integrations.map((it) => it.id));
    const existing: string[] = settings?.permissions?.allow ?? [];
    const kept = existing.filter((entry: string) => {
        if (!entry.startsWith("mcp__") || entry.startsWith("mcp__plugin_")) return true;
        const m = entry.match(/^mcp__([^_]+(?:_[^_]+)*?)__/);
        if (!m) return true;
        return !managedIds.has(m[1]);
    });

    const added: string[] = [];
    const grantedSet = new Set<string>();
    for (const g of grants) {
        const [server, ...rest] = g.split("__");
        if (!server || rest.length === 0) continue;
        if (!managedIds.has(server)) continue; // silently ignore tampering
        const tool = rest.join("__");
        added.push(`mcp__${server}__${tool}`);
        grantedSet.add(`${server}__${tool}`);
    }

    // Operator intent must be authoritative: tools the operator did NOT tick
    // go into permissions.deny so claude refuses without prompting. Without
    // this, untouched tools fall through to claude's default ask-the-user
    // behavior, and the telegram plugin surfaces the prompt as inline buttons —
    // letting the user grant a permission the operator deliberately withheld.
    const allDenied: string[] = [];
    for (const it of integrations) {
        const manifest = loadManifest(it.id);
        const toolNames = manifest?.tools ? Object.keys(manifest.tools) : [];
        for (const toolName of toolNames) {
            if (grantedSet.has(`${it.id}__${toolName}`)) continue;
            allDenied.push(`mcp__${it.id}__${toolName}`);
        }
    }
    const existingDeny: string[] = settings?.permissions?.deny ?? [];
    const keptDeny = existingDeny.filter((entry: string) => {
        if (!entry.startsWith("mcp__") || entry.startsWith("mcp__plugin_")) return true;
        const m = entry.match(/^mcp__([^_]+(?:_[^_]+)*?)__/);
        if (!m) return true;
        return !managedIds.has(m[1]);
    });

    settings.permissions ??= {};
    settings.permissions.allow = [...new Set([...kept, ...added])];
    settings.permissions.deny = [...new Set([...keptDeny, ...allDenied])];

    try {
        const fs = await import("node:fs");
        const tmp = `${settingsPath}.tmp`;
        fs.writeFileSync(tmp, JSON.stringify(settings, null, 2), { encoding: "utf8" });
        fs.renameSync(tmp, settingsPath);
    } catch (e) {
        return c.html(errorPage("Failed to write settings.json", String(e)));
    }
    // Re-set ownership to the agent — atomic write may have left it root-owned
    spawnSync("/bin/chown", [`${name}:${name}`, settingsPath]);

    // Refresh systemd drop-in so the right credentials are loaded into
    // $CREDENTIALS_DIRECTORY at service start (otherwise the MCP server
    // crashes trying to read /etc/agents/credentials/*.cred directly).
    await writeSystemdDropIn(name);
    // Refresh .mcp.json so claude discovers any newly-activated integrations
    await writeMcpJson(name);
    // Refresh agent.toml's [tools].enabled / [permissions] so agent-mcp-launcher
    // (which gates on agent.toml, not settings.json) will spawn the granted servers.
    await writeAgentToml(name);

    // Restart agent so the new permission set + drop-in + .mcp.json are read
    spawnSync("systemctl", ["restart", `agent@${name}.service`]);
    return c.redirect(`/agent/${name}/permissions?saved=1`);
});

function errorPage(title: string, detail = ""): string {
    return layout("Error", card(`
        ${pageHeader(title, detail)}
        ${button("Back", { href: "/", intent: "secondary" })}
    `));
}

// ─── boot ─────────────────────────────────────────────────────────────────

console.log(`AgentHQ wizard listening on http://localhost:${PORT}`);
export default { port: PORT, fetch: app.fetch };
