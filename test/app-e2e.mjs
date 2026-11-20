// End-to-end test of the app tools (files + services through claude-app-helper): node test/app-e2e.mjs
// Runs the helper as an unprivileged user with a test config, fake systemctl/journalctl and a fake /opt app.
import { spawn, execSync } from "node:child_process";
import { mkdirSync, writeFileSync, readFileSync, rmSync, chmodSync } from "node:fs";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const R = "/tmp/fakeapp"; rmSync(R, { recursive: true, force: true });
for (const d of ["opt/bcu-livekit/recordings", "opt/bcu-livekit/.git", "backups", "bin"]) mkdirSync(`${R}/${d}`, { recursive: true });
writeFileSync(`${R}/opt/bcu-livekit/agent.py`, 'import os\nFISH = os.environ.get("FISH_AUDIO_API_KEY")\nGREETING = "Hi"\nOPENAI_API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n');
writeFileSync(`${R}/opt/bcu-livekit/sweetspot_layered.py`, 'def plan():\n    return "Anything else?"\n');
writeFileSync(`${R}/opt/bcu-livekit/.env`, "FISH_AUDIO_API_KEY=supersecretvalue123\n");
writeFileSync(`${R}/opt/bcu-livekit/recordings/call1.ogg`, "x");
writeFileSync(`${R}/bin/systemctl`, `#!/bin/sh\ncase "$1" in show) echo "ActiveState=active"; echo "SubState=running";; is-active) echo active;; restart) echo "restarted $2" >> ${R}/restarts;; esac\n`);
writeFileSync(`${R}/bin/journalctl`, `#!/bin/sh\necho "2026-11-20T08:00:00 agent[1]: call started token=abcdef1234567890"\necho "2026-11-20T08:00:01 agent[1]: fish first audio 140ms"\n`);
chmodSync(`${R}/bin/systemctl`, 0o755); chmodSync(`${R}/bin/journalctl`, 0o755);
writeFileSync(`${R}/helper.conf`, `dirs = ${R}/opt/bcu-livekit\nunits = bcu-livekit.service\nbackup_dir = ${R}/backups\nsystemctl = ${R}/bin/systemctl\njournalctl = ${R}/bin/journalctl\npython = python3\n`);
execSync(`chown -R apptest ${R}`);

const TOKEN = "t".repeat(40), PORT = 18788;
const HELPER = `setpriv --reuid=apptest --regid=apptest --clear-groups env CLAUDE_APP_HELPER_CONF=${R}/helper.conf python3 ${process.cwd()}/setup/claude-app-helper.py`;
const env = { ...process.env, CONNECTOR_TOKEN: TOKEN, PORT: String(PORT), RUN_MODE: "local", APP_HELPER_CMD: HELPER,
  ASTERISK_CONF_DIR: "/tmp/fakeast/etc", ASTERISK_LOG_DIR: "/tmp/fakeast/log", ASTERISK_CMD: "/tmp/fakeast/bin/asterisk" };
const srv = spawn("node", ["dist/server.js"], { env, stdio: ["ignore", "pipe", "inherit"] });
const logs = []; srv.stdout.on("data", (d) => logs.push(String(d)));
await new Promise((r) => setTimeout(r, 800));

const call = async (c, name, args = {}) => { const r = await c.callTool({ name, arguments: args }); return { err: !!r.isError, t: r.content.map((x) => x.text).join("\n") }; };
let failed = 0; const t = async (name, fn) => { try { await fn(); console.log("ok  ", name); } catch (e) { failed++; console.log("FAIL", name, "\n   ", e.message); } };
const A = `${R}/opt/bcu-livekit`;

try {
  const c = new Client({ name: "test", version: "1" });
  await c.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${PORT}/mcp/${TOKEN}`)));
  await t("app tools listed", async () => { const { tools } = await c.listTools(); assert.equal(tools.length, 19); });
  await t("list hides .env, recordings, .git", async () => {
    const r = await call(c, "list_app_files"); assert.match(r.t, /agent\.py/); assert.doesNotMatch(r.t, /\.env|call1\.ogg|\.git/);
  });
  let sha;
  await t("read masks secrets", async () => {
    const r = await call(c, "read_app_file", { path: `${A}/agent.py` }); assert.ok(!r.err, r.t);
    assert.match(r.t, /secrets masked/); assert.doesNotMatch(r.t, /sk-abcdef/); assert.match(r.t, /GREETING = "Hi"/);
    sha = r.t.match(/sha256 ([0-9a-f]{64})/)[1];
  });
  await t("relative path works", async () => { const r = await call(c, "read_app_file", { path: "sweetspot_layered.py" }); assert.ok(!r.err, r.t); });
  await t(".env refused", async () => { const r = await call(c, "read_app_file", { path: `${A}/.env` }); assert.ok(r.err); assert.match(r.t, /protected/); });
  await t("recording refused", async () => { const r = await call(c, "read_app_file", { path: `${A}/recordings/call1.ogg` }); assert.ok(r.err); });
  await t("outside folders refused", async () => { for (const p of ["/etc/passwd", `${A}/../helper.conf`, "/etc/asterisk-connector.env"]) { const r = await call(c, "read_app_file", { path: p }); assert.ok(r.err, p); } });
  await t("edit keeps hidden secret, backs up, diffs", async () => {
    const r = await call(c, "edit_app_file", { path: `${A}/agent.py`, old_text: 'GREETING = "Hi"', new_text: 'GREETING = "Hello"', expected_sha256: sha });
    assert.ok(!r.err, r.t); assert.match(r.t, /Backup: .*agent\.py\.\d{8}T\d{6}Z/); assert.match(r.t, /\+GREETING = "Hello"/); assert.match(r.t, /restart_service/);
    assert.match(readFileSync(`${A}/agent.py`, "utf8"), /sk-abcdefghijklmnopqrstuvwxyz123456/);
  });
  await t("stale sha refused", async () => { const r = await call(c, "edit_app_file", { path: `${A}/agent.py`, old_text: "GREETING", new_text: "GREETING", expected_sha256: sha }); assert.ok(r.err); assert.match(r.t, /modified since/); });
  await t("broken python not saved", async () => {
    const r = await call(c, "edit_app_file", { path: `${A}/sweetspot_layered.py`, old_text: 'return "Anything else?"', new_text: 'return "Anything else?"(' });
    assert.ok(r.err); assert.match(r.t, /would not compile/); assert.match(readFileSync(`${A}/sweetspot_layered.py`, "utf8"), /return "Anything else\?"\n/);
  });
  await t("editing masked value refused", async () => { const r = await call(c, "edit_app_file", { path: `${A}/agent.py`, old_text: "«hidden»", new_text: "x" }); assert.ok(r.err); });
  await t("adding a secret refused", async () => { const r = await call(c, "edit_app_file", { path: `${A}/sweetspot_layered.py`, old_text: "def plan():", new_text: 'API_KEY = "abcdefghijk123"\ndef plan():' }); assert.ok(r.err); });
  await t("whole-file replace of secret file refused", async () => {
    const cur = await call(c, "read_app_file", { path: `${A}/agent.py` }); const s = cur.t.match(/sha256 ([0-9a-f]{64})/)[1];
    const r = await call(c, "write_app_file", { path: `${A}/agent.py`, content: "x = 1\n", expected_sha256: s }); assert.ok(r.err); assert.match(r.t, /only be changed with edit/);
  });
  await t("new file", async () => { const r = await call(c, "write_app_file", { path: `${A}/fish_check.py`, content: "print('ok')\n" }); assert.ok(!r.err, r.t); assert.match(r.t, /new file/); });
  await t("new binary-ish name refused", async () => { const r = await call(c, "write_app_file", { path: `${A}/x.bin`, content: "x" }); assert.ok(r.err); });
  let bk;
  await t("backups + restore", async () => {
    const l = await call(c, "list_app_backups", { path: `${A}/agent.py` }); bk = l.t.split("\n")[0]; assert.match(bk, /agent\.py\.\d{8}T\d{6}Z$/);
    const r = await call(c, "restore_app_file", { path: `${A}/agent.py`, backup: bk }); assert.ok(!r.err, r.t);
    assert.match(readFileSync(`${A}/agent.py`, "utf8"), /GREETING = "Hi"/);
  });
  await t("services", async () => { const r = await call(c, "server_services"); assert.match(r.t, /bcu-livekit\.service/); assert.match(r.t, /ActiveState=active/); });
  await t("logs mask secrets + filter", async () => {
    const r = await call(c, "service_logs", { service: "bcu-livekit.service", lines: 50 }); assert.match(r.t, /fish first audio/); assert.doesNotMatch(r.t, /abcdef1234567890/);
    const f = await call(c, "service_logs", { service: "bcu-livekit.service", lines: 50, filter: "FISH" }); assert.doesNotMatch(f.t, /call started/);
  });
  await t("unknown service refused", async () => { for (const s of ["sshd.service", "asterisk.service; rm -rf /"]) { const r = await call(c, "restart_service", { service: s }); assert.ok(r.err, s); } });
  await t("restart allowed service", async () => {
    const r = await call(c, "restart_service", { service: "bcu-livekit.service" }); assert.ok(!r.err, r.t); assert.match(r.t, /now active/);
    assert.match(readFileSync(`${R}/restarts`, "utf8"), /bcu-livekit\.service/);
  });
  await t("audit log has no file contents", async () => { const l = logs.join(""); assert.ok(l.includes('"tool":"edit_app_file"')); assert.ok(!l.includes("GREETING = \\\"Hello")); });
  await c.close();
} finally { srv.kill(); }
console.log(failed ? `\n${failed} FAILED` : "\nall app tests passed");
process.exit(failed ? 1 : 0);
