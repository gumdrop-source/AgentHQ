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

app.get("/integrations", (c) => c.html(layout("Integrations", card(`
    ${pageHeader("Integrations", "Activate the tools your agents can use.")}
    <p class="text-slate-600">M365, Gmail, MYOB, Xero, Hikvision, Home Assistant, PayPal, Vapi, Twilio…</p>
    <p class="mt-3 text-sm text-slate-500"><em>Coming soon.</em> Each integration will have a guided setup wizard — instructions for registering the external service, prompts for credentials, validation, and one-click activation. Once activated, every agent on this host has access to it.</p>
`), "integrations", c.get("user"))));

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

// Permissions matrix: read-only first cut. Reads the agent's settings.json
// allow list, groups MCP entries by server, and decorates them with tool
// metadata from /opt/agents/tools/<server>/tool.json (if present).
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

    // Bucket the allow list:
    //   builtin    Bash/Read/Write/etc — always granted, not editable
    //   plugin     mcp__plugin_*       — always granted (telegram, claude-mem)
    //   mcp        mcp__<server>__<tool> — per-server permissions
    const builtins: string[] = [];
    const plugins: Record<string, string[]> = {};
    const mcp: Record<string, Set<string>> = {};
    for (const entry of allow) {
        if (!entry.startsWith("mcp__")) {
            builtins.push(entry);
            continue;
        }
        // mcp__<server>__<tool>  or  mcp__<server>__*
        const m = entry.match(/^mcp__([^_]+(?:_[^_]+)*?)__(.+)$/);
        if (!m) continue;
        const [, server, tool] = m;
        if (server.startsWith("plugin_")) {
            plugins[server] ??= [];
            plugins[server].push(tool);
        } else {
            mcp[server] ??= new Set();
            mcp[server].add(tool);
        }
    }

    // Load tool manifests from /opt/agents/tools/*/tool.json so the matrix
    // shows tool descriptions, marks destructive ones, etc.
    const manifests: Record<string, any> = {};
    try {
        const toolsDir = "/opt/agents/tools";
        for (const dir of readdirSync(toolsDir)) {
            const path = `${toolsDir}/${dir}/tool.json`;
            if (existsSync(path)) {
                try { manifests[dir] = JSON.parse(readFileSync(path, "utf8")); } catch {}
            }
        }
    } catch {}

    // Render
    const builtinList = builtins.length === 0
        ? `<p class="text-sm text-slate-500 italic">none</p>`
        : `<div class="flex flex-wrap gap-1.5">${builtins.map((b) =>
            `<span class="inline-block px-2 py-0.5 rounded bg-slate-100 text-slate-700 text-xs font-mono">${escapeHtml(b)}</span>`
          ).join("")}</div>`;

    const pluginsList = Object.keys(plugins).length === 0
        ? `<p class="text-sm text-slate-500 italic">none</p>`
        : Object.entries(plugins).map(([server, tools]) => `
            <div class="mb-3">
              <p class="text-sm font-medium font-mono">${escapeHtml(server)}</p>
              <div class="flex flex-wrap gap-1.5 mt-1">${tools.map((t) =>
                `<span class="inline-block px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 text-xs font-mono">${escapeHtml(t)}</span>`
              ).join("")}</div>
            </div>`).join("");

    const mcpServers = Object.keys(mcp).sort();
    const mcpSection = mcpServers.length === 0
        ? `<p class="text-sm text-slate-500 italic">No MCP integrations granted yet. Activate one in <a href="/integrations" class="underline">Integrations</a>, then return here to grant tools.</p>`
        : mcpServers.map((server) => {
            const granted = mcp[server];
            const manifest = manifests[server];
            const allTools = manifest?.tools ?? {};
            const known = Object.keys(allTools);
            const rows = known.length > 0
                ? known.map((tool) => {
                    const meta = allTools[tool] ?? {};
                    const isGranted = granted.has(tool) || granted.has("*");
                    const dot = isGranted ? "bg-emerald-500" : "bg-slate-200";
                    const destructive = meta.destructive ? `<span class="ml-2 text-xs text-rose-600">⚠ destructive</span>` : "";
                    return `
                        <li class="flex items-center justify-between py-1.5 border-b border-slate-100 last:border-0">
                          <div class="flex items-center gap-3">
                            <span class="w-2 h-2 rounded-full ${dot}"></span>
                            <span class="font-mono text-sm">${escapeHtml(tool)}</span>
                            ${destructive}
                          </div>
                          <span class="text-xs text-slate-500">${escapeHtml(meta.description ?? "")}</span>
                        </li>`;
                  }).join("")
                : Array.from(granted).map((tool) => `
                    <li class="flex items-center gap-3 py-1.5 border-b border-slate-100 last:border-0">
                      <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
                      <span class="font-mono text-sm">${escapeHtml(tool)}</span>
                      <span class="text-xs text-slate-400">(no manifest)</span>
                    </li>`).join("");
            return `
                <div class="bg-white rounded-xl border border-slate-200 p-5 mb-4">
                  <div class="flex items-baseline justify-between mb-2">
                    <h3 class="font-medium">${escapeHtml(manifest?.title ?? server)}</h3>
                    <span class="text-xs text-slate-500 font-mono">${escapeHtml(server)}</span>
                  </div>
                  ${manifest?.description ? `<p class="text-sm text-slate-600 mb-3">${escapeHtml(manifest.description)}</p>` : ""}
                  <ul>${rows}</ul>
                </div>`;
        }).join("");

    return c.html(layout(`${name} — permissions`, `
        <div class="flex items-center justify-between mb-6">
          <div>
            <h1 class="text-2xl font-semibold tracking-tight">${escapeHtml(name)} permissions</h1>
            <p class="text-slate-600 text-sm">What this agent is allowed to do.</p>
          </div>
          ${button("Back to agent", { href: `/agent/${name}`, intent: "secondary" })}
        </div>

        <div class="bg-white rounded-xl border border-slate-200 p-5 mb-4">
          <h3 class="font-medium mb-2">Always granted</h3>
          <p class="text-xs text-slate-500 mb-3">Built-in tools and plugin tools — every agent gets these.</p>
          <p class="text-sm font-medium mt-2 mb-1">Built-ins</p>
          ${builtinList}
          <p class="text-sm font-medium mt-3 mb-1">Plugin tools</p>
          ${pluginsList}
        </div>

        <h2 class="text-lg font-medium mb-3 mt-6">MCP integrations</h2>
        ${mcpSection}

        <p class="text-xs text-slate-500 mt-6">
          Editable matrix coming soon. For now this is read-only —
          edit <code>/home/${escapeHtml(name)}/.claude/settings.json</code> directly and restart the service.
        </p>
    `, "agents", c.get("user")));
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
