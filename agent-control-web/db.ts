// db.ts — SQLite + migrations layer for agent-control-web.
//
// Storage: /var/lib/agent-control/db.sqlite (Phase 10 of the bootstrap
// creates that directory). Falls back to ./dev.sqlite for local dev.
//
// Migrations: every .sql file in schema/ is run once and tracked by version
// number in schema_migrations. New migrations land as 00N_xxx.sql with N
// monotonically incremented; never edit a migration after it's shipped.

import { Database } from "bun:sqlite";
import { existsSync, mkdirSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

const DB_PATH = process.env.AGENT_CONTROL_DB ?? (
    existsSync("/var/lib/agent-control")
        ? "/var/lib/agent-control/db.sqlite"
        : "./dev.sqlite"
);
const SCHEMA_DIR = join(import.meta.dir, "schema");

// Ensure parent dir exists
const parent = dirname(DB_PATH);
if (!existsSync(parent)) {
    try { mkdirSync(parent, { recursive: true }); } catch {}
}

export const db = new Database(DB_PATH, { create: true });
db.exec("PRAGMA journal_mode = WAL");
db.exec("PRAGMA foreign_keys = ON");

// Bootstrap the migrations bookkeeping table
db.exec(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    INTEGER PRIMARY KEY,
        filename   TEXT    NOT NULL,
        applied_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );
`);

function applyPendingMigrations() {
    if (!existsSync(SCHEMA_DIR)) return;
    const files = readdirSync(SCHEMA_DIR)
        .filter((f) => /^\d{3}_.*\.sql$/.test(f))
        .sort();

    const applied = new Set<number>(
        db.query<{ version: number }, []>("SELECT version FROM schema_migrations").all().map((r) => r.version),
    );

    for (const file of files) {
        const version = parseInt(file.slice(0, 3), 10);
        if (applied.has(version)) continue;
        const sql = readFileSync(join(SCHEMA_DIR, file), "utf8");
        console.log(`[db] applying migration ${file}`);
        db.transaction(() => {
            db.exec(sql);
            db.run("INSERT INTO schema_migrations (version, filename) VALUES (?, ?)", [version, file]);
        })();
    }
}

applyPendingMigrations();

console.log(`[db] open at ${DB_PATH}`);

// ─── helpers ──────────────────────────────────────────────────────────────

export function userCount(): number {
    const r = db.query<{ n: number }, []>("SELECT COUNT(*) AS n FROM users").get();
    return r?.n ?? 0;
}

export interface User {
    id: number;
    email: string;
    password_hash: string;
    display_name: string;
    role: "admin" | "user";
    created_at: string;
}

export function userByEmail(email: string): User | null {
    return db.query<User, [string]>("SELECT * FROM users WHERE email = ?").get(email) ?? null;
}

export function userById(id: number): User | null {
    return db.query<User, [number]>("SELECT * FROM users WHERE id = ?").get(id) ?? null;
}

export async function createUser(email: string, password: string, displayName: string, role: "admin" | "user"): Promise<number> {
    const hash = await Bun.password.hash(password, { algorithm: "argon2id" });
    const r = db.run(
        "INSERT INTO users (email, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
        [email.toLowerCase().trim(), hash, displayName.trim(), role],
    );
    return Number(r.lastInsertRowid);
}

export async function verifyPassword(user: User, password: string): Promise<boolean> {
    return Bun.password.verify(password, user.password_hash);
}

// ─── sessions ─────────────────────────────────────────────────────────────

export interface Session {
    id: string;
    user_id: number;
    created_at: string;
    expires_at: string;
}

const SESSION_TTL_DAYS = 30;

export function createSession(userId: number): string {
    const id = crypto.randomUUID() + "-" + crypto.randomUUID();
    const expires = new Date(Date.now() + SESSION_TTL_DAYS * 86400 * 1000).toISOString();
    db.run(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        [id, userId, expires],
    );
    return id;
}

export function userBySession(sessionId: string): User | null {
    const row = db.query<{ user_id: number; expires_at: string }, [string]>(
        "SELECT user_id, expires_at FROM sessions WHERE id = ?",
    ).get(sessionId);
    if (!row) return null;
    if (new Date(row.expires_at) < new Date()) {
        db.run("DELETE FROM sessions WHERE id = ?", [sessionId]);
        return null;
    }
    return userById(row.user_id);
}

export function destroySession(sessionId: string): void {
    db.run("DELETE FROM sessions WHERE id = ?", [sessionId]);
}
