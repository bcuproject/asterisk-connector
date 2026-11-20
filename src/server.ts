import { timingSafeEqual } from "node:crypto";
import express from "express";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { Asterisk, RELOADS, UserError, applyEdit, configFromEnv, reloadFor } from "./asterisk.js";
import { LocalRunner, type Runner, SshRunner } from "./runner.js";

const TOKEN = process.env.CONNECTOR_TOKEN || "";
if (TOKEN.length < 32) { console.error("CONNECTOR_TOKEN must be at least 32 characters (try: openssl rand -hex 32)."); process.exit(1); }
const runner: Runner = process.env.RUN_MODE === "local" ? new LocalRunner() : SshRunner.fromEnv();
const cfg = configFromEnv();
const ast = new Asterisk(runner, cfg);
const PORT = Number(process.env.PORT || 8787);

const audit = (tool: string, args: unknown, ok: boolean, note = "") =>
  console.log(JSON.stringify({ t: new Date().toISOString(), tool, args, ok, note }));
const text = (s: string) => ({ content: [{ type: "text" as const, text: s }] });
const fail = (s: string) => ({ content: [{ type: "text" as const, text: s }], isError: true });

const SECRET_KEYS = new Set(["content", "old_text", "new_text"]);
/** Wraps a tool handler: audit log + friendly errors. Config file contents are never logged. */
function wrap<A extends Record<string, unknown>>(name: string, fn: (a: A) => Promise<ReturnType<typeof text>>) {
  return async (a: A) => {
    const logged = Object.fromEntries(Object.entries(a ?? {}).map(([k, v]) => [k, typeof v === "string" && (SECRET_KEYS.has(k) || v.length > 120) ? `<${v.length} chars>` : v]));
    try { const r = await fn(a); audit(name, logged, true); return r; }
    catch (e) { const m = e instanceof Error ? e.message : String(e); audit(name, logged, false, m); return fail(e instanceof UserError ? m : `Error: ${m}`); }
  };
}

async function afterWrite(file: string, reload: boolean, rollbackOnError: boolean, res: { backup: string | null; diff: string }) {
  let out = `Saved ${file}.${res.backup ? ` Backup: ${res.backup}` : ""}\n\n${res.diff}`;
  const mod = reloadFor(file);
  if (!reload) return out + `\n\nNot reloaded. ${mod ? `Run reload with module "${mod}" to apply.` : ""}`;
  if (!mod) return out + `\n\nNo matching reload for this file; use the reload tool (module "all" = core reload) if needed.`;
  const r = await ast.reload(mod);
  out += `\n\nReloaded ${mod}: ${r.output || "ok"}`;
  if (r.problems.length) {
    out += `\n\nNew warnings/errors in the log after reload:\n${r.problems.join("\n")}`;
    if (rollbackOnError && res.backup && r.problems.some((l) => /\bERROR\b/.test(l))) {
      await ast.restore(file, res.backup);
      const again = await ast.reload(mod);
      out += `\n\nERRORS found, so the previous version was restored and ${mod} reloaded again (${again.output || "ok"}). Nothing changed on the live system.`;
    }
  } else out += "\nNo new warnings or errors in the log.";
  return out;
}

function buildServer() {
  const s = new McpServer({ name: "asterisk-connector", version: "1.0.0" }, {
    instructions: "Manages an Asterisk PBX. Always read a config file before editing it and prefer edit_config over write_config. " +
      "Explain the change to the user before saving. Changes are backed up automatically and the matching module is reloaded; " +
      "check the reported warnings. Use restore_backup to undo. CLI access is read-only.",
  });

  s.registerTool("server_status", { title: "Asterisk status", description: "Asterisk version, uptime and active call count.", inputSchema: {}, annotations: { readOnlyHint: true } },
    wrap("server_status", async () => text(await ast.status())));

  s.registerTool("cli", {
    title: "Run read-only Asterisk CLI", annotations: { readOnlyHint: true },
    description: "Run a read-only Asterisk CLI command, e.g. 'pjsip show endpoints', 'pjsip show contacts', 'core show channels', 'dialplan show from-internal', 'queue show', 'sip show peers', 'database show'. Changes are not allowed here.",
    inputSchema: { command: z.string().describe("CLI command, without 'asterisk -rx'") },
  }, wrap("cli", async ({ command }: { command: string }) => text(await ast.cli(command) || "(no output)")));

  s.registerTool("list_configs", { title: "List config files", description: `List config files in ${cfg.confDir} (name, size, last modified).`, inputSchema: {}, annotations: { readOnlyHint: true } },
    wrap("list_configs", async () => text(await ast.listConfigs())));

  s.registerTool("read_config", {
    title: "Read a config file", annotations: { readOnlyHint: true },
    description: "Read a config file from /etc/asterisk, e.g. extensions.conf or pjsip.conf. Returns contents with line numbers and a sha256 to pass to write_config.",
    inputSchema: { file: z.string(), from_line: z.number().int().min(1).optional(), to_line: z.number().int().min(1).optional() },
  }, wrap("read_config", async ({ file, from_line, to_line }: { file: string; from_line?: number; to_line?: number }) => {
    const { content, sha256 } = await ast.read(file);
    const lines = content.split("\n"); const a = (from_line ?? 1) - 1, b = Math.min(lines.length, to_line ?? lines.length);
    const body = lines.slice(a, b).map((l, i) => `${String(a + i + 1).padStart(5)}  ${l}`).join("\n");
    return text(`# ${file} (${lines.length} lines, sha256 ${sha256})\n${body}`);
  }));

  s.registerTool("edit_config", {
    title: "Edit a config file", annotations: { destructiveHint: false },
    description: "Replace an exact piece of text in a config file (copy old_text from read_config without the line numbers). Backs up first, shows a diff, then reloads the matching module and reports new log warnings. If reload logs an ERROR, it automatically restores the previous version unless rollback_on_error is false.",
    inputSchema: { file: z.string(), old_text: z.string(), new_text: z.string(), replace_all: z.boolean().default(false), reload: z.boolean().default(true), rollback_on_error: z.boolean().default(true) },
  }, wrap("edit_config", async (a: { file: string; old_text: string; new_text: string; replace_all: boolean; reload: boolean; rollback_on_error: boolean }) => {
    const { content, sha256 } = await ast.read(a.file);
    const next = applyEdit(content, a.old_text, a.new_text, a.replace_all);
    const res = await ast.write(a.file, next, sha256);
    return text(await afterWrite(a.file, a.reload, a.rollback_on_error, res));
  }));

  s.registerTool("write_config", {
    title: "Write a whole config file", annotations: { destructiveHint: true },
    description: "Create a new config file or replace a whole one. For existing files pass expected_sha256 from read_config. Prefer edit_config for small changes. Backs up, reloads and rolls back on ERROR like edit_config.",
    inputSchema: { file: z.string(), content: z.string(), expected_sha256: z.string().optional(), reload: z.boolean().default(true), rollback_on_error: z.boolean().default(true) },
  }, wrap("write_config", async (a: { file: string; content: string; expected_sha256?: string; reload: boolean; rollback_on_error: boolean }) => {
    let exists = true;
    try { await ast.read(a.file); } catch { exists = false; }
    if (exists && !a.expected_sha256) throw new UserError("This file exists. Read it first and pass expected_sha256, or use edit_config.");
    const res = await ast.write(a.file, a.content, a.expected_sha256 ?? null);
    return text(await afterWrite(a.file, a.reload, a.rollback_on_error, res));
  }));

  s.registerTool("reload", {
    title: "Reload an Asterisk module", annotations: { destructiveHint: false },
    description: "Reload part of Asterisk so config changes take effect, then report new log warnings/errors.",
    inputSchema: { module: z.enum(Object.keys(RELOADS) as [string, ...string[]]) },
  }, wrap("reload", async ({ module }: { module: string }) => {
    const r = await ast.reload(module);
    return text(`${RELOADS[module]}: ${r.output || "ok"}\n${r.problems.length ? `\nNew warnings/errors:\n${r.problems.join("\n")}` : "\nNo new warnings or errors in the log."}`);
  }));

  s.registerTool("list_backups", { title: "List backups", description: "List automatic backups (newest first), optionally for one file.", inputSchema: { file: z.string().optional() }, annotations: { readOnlyHint: true } },
    wrap("list_backups", async ({ file }: { file?: string }) => text(await ast.listBackups(file))));

  s.registerTool("restore_backup", {
    title: "Restore a backup", annotations: { destructiveHint: true },
    description: "Put a backed-up version of a config file back (the current version is backed up first), then reload.",
    inputSchema: { file: z.string(), backup: z.string().describe("Name from list_backups"), reload: z.boolean().default(true) },
  }, wrap("restore_backup", async (a: { file: string; backup: string; reload: boolean }) => {
    const res = await ast.restore(a.file, a.backup);
    return text(await afterWrite(a.file, a.reload, false, res));
  }));

  s.registerTool("tail_log", {
    title: "Read the Asterisk log", annotations: { readOnlyHint: true },
    description: "Last lines of an Asterisk log in /var/log/asterisk (default 'messages'; also 'full', 'queue_log', 'security' if enabled), optionally filtered by text (case-insensitive), e.g. an extension, peer or 'ERROR'.",
    inputSchema: { lines: z.number().int().min(1).max(1000).default(100), filter: z.string().optional(), log: z.string().default("messages") },
  }, wrap("tail_log", async (a: { lines: number; filter?: string; log: string }) => text(await ast.tailLog(a.lines, a.filter, a.log))));

  return s;
}

const tokenOk = (t: string | undefined) => {
  if (!t) return false;
  const a = Buffer.from(t), b = Buffer.from(TOKEN);
  return a.length === b.length && timingSafeEqual(a, b);
};

process.on("uncaughtException", (e) => console.error("[uncaught]", e));
process.on("unhandledRejection", (e) => console.error("[unhandled]", e));

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "1mb" }));
app.get("/health", (_req, res) => { res.json({ ok: true }); });

// Simple per-IP rate limit: 120 requests / minute.
const hits = new Map<string, { n: number; t: number }>();
app.use((req, res, next) => {
  const ip = req.ip || "?"; const now = Date.now(); const h = hits.get(ip);
  if (!h || now - h.t > 60_000) hits.set(ip, { n: 1, t: now }); else if (++h.n > 120) { res.status(429).end(); return; }
  next();
});

async function handle(req: express.Request, res: express.Response) {
  const bearer = req.headers.authorization?.replace(/^Bearer\s+/i, "");
  if (!tokenOk(req.params.token as string | undefined) && !tokenOk(bearer)) { await new Promise((r) => setTimeout(r, 500)); res.status(404).end(); return; }
  if (req.method !== "POST") { res.status(405).set("Allow", "POST").end(); return; }
  const server = buildServer();
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  res.on("close", () => { transport.close(); server.close(); });
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
}
app.all("/mcp/:token", handle);
app.all("/mcp", handle);

app.listen(PORT, process.env.BIND || "127.0.0.1", () => console.log(`asterisk-connector on :${PORT} using ${runner.describe()} (${cfg.allowWrite ? "read+write" : "read-only"})`));
