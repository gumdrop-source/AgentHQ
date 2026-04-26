// AgentHQ setup wizard — single-file Hono server.
//
// Runs as root on localhost:5000 (config via PORT env). Wraps the bash CLIs
// (agent-control, agenthq-cred, systemctl) and presents a step-by-step
// browser flow. Server-rendered HTML, no client framework. Tailwind via CDN.
//
// Runtime: bun. Started by /etc/systemd/system/agent-control-web.service.

import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";

const app = new Hono();
const PORT = Number(process.env.PORT ?? 5000);

// ─── HTML helpers ─────────────────────────────────────────────────────────

const layout = (title: string, body: string) => `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${title} · AgentHQ</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@2.0.4" integrity="sha384-HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"></script>
  <style>body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}</style>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen">
  <div class="max-w-3xl mx-auto px-6 py-10">
    <header class="mb-8 flex items-center justify-between">
      <a href="/" class="text-xl font-semibold tracking-tight">AgentHQ</a>
      <span class="text-xs text-slate-500">setup wizard</span>
    </header>
    ${body}
  </div>
</body>
</html>`;

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

const code = (s: string) => `<pre class="bg-slate-100 rounded-lg p-3 text-sm font-mono overflow-x-auto">${escapeHtml(s)}</pre>`;

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

app.get("/", (c) => {
    const agents = listAgents();
    const agentList = agents.length === 0
        ? `<p class="text-slate-500 italic">No agents yet.</p>`
        : `<ul class="space-y-2">${agents.map((a) => `
            <li class="flex items-center justify-between border-b border-slate-100 last:border-0 pb-2">
              <span class="font-mono">${escapeHtml(a.name)}</span>
              <span class="text-sm ${a.status === "active" ? "text-emerald-600" : "text-slate-500"}">${escapeHtml(a.status)}</span>
            </li>`).join("")}</ul>`;

    return c.html(layout("Welcome", card(`
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
          <section>
            <h2 class="font-medium mb-2">Existing agents</h2>
            ${agentList}
          </section>
          <div class="pt-2">
            ${button("Create your first agent", { href: "/setup/agent" })}
          </div>
        </div>
    `)));
});

app.get("/setup/agent", (c) => {
    return c.html(layout("Create agent", card(`
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
    `)));
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
    `)));
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

        for await (const chunk of child.stdout) await s.writeSSE({ event: "line", data: chunk.toString() });
        for await (const chunk of child.stderr) await s.writeSSE({ event: "line", data: chunk.toString() });
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
            : `<p class="mb-3">Open a terminal on this box and run:</p>
               ${code(`sudo -i -u ${name} claude`)}
               <p class="my-3 text-sm text-slate-600">Claude will print a URL — open it in a browser, sign in to your Anthropic account, and paste the code back. Then type <code>/exit</code>.</p>
               <p class="text-sm text-slate-500">This page checks every few seconds…</p>
               <script>setTimeout(() => location.reload(), 4000)</script>`}
    `)));
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
    `)));
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

    const r2 = spawnSync("systemctl", ["start", `agent@${name}.service`], { encoding: "utf8" });
    if (r2.status !== 0) return c.html(errorPage("Service failed to start", r2.stderr || ""));

    return c.redirect(`/agent/${name}`);
});

app.get("/agent/:name", (c) => {
    const name = c.req.param("name");
    const r = spawnSync("systemctl", ["status", `agent@${name}.service`, "--no-pager"], { encoding: "utf8" });
    return c.html(layout(name, card(`
        ${pageHeader(`Agent: ${escapeHtml(name)}`)}
        ${code(r.stdout || r.stderr)}
        <p class="mt-4 text-sm text-slate-600">If status is <code>active (running)</code>, message your bot in Telegram. It should reply.</p>
        <div class="mt-4 flex gap-3">
          ${button("Refresh", { href: `/agent/${name}`, intent: "secondary" })}
          ${button("Back to dashboard", { href: "/", intent: "secondary" })}
        </div>
    `)));
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
