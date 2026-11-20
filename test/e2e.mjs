// End-to-end test against a fake Asterisk: node test/e2e.mjs  (needs /tmp/fakeast from test/fake-asterisk.sh)
import { spawn, execSync } from "node:child_process";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

execSync("sh test/fake-asterisk.sh");
const TOKEN = "t".repeat(40), PORT = 18787;
const env = { ...process.env, CONNECTOR_TOKEN: TOKEN, PORT: String(PORT), RUN_MODE: process.env.RUN_MODE || "local",
  ASTERISK_CONF_DIR: "/tmp/fakeast/etc", ASTERISK_LOG_DIR: "/tmp/fakeast/log", ASTERISK_CMD: "/tmp/fakeast/bin/asterisk" };
const srv = spawn("node", ["dist/server.js"], { env, stdio: ["ignore", "pipe", "inherit"] });
const logs = []; srv.stdout.on("data", (d) => logs.push(String(d)));
await new Promise((r) => setTimeout(r, 800));

const call = async (c, name, args = {}) => { const r = await c.callTool({ name, arguments: args }); return { err: !!r.isError, t: r.content.map((x) => x.text).join("\n") }; };
let failed = 0; const t = async (name, fn) => { try { await fn(); console.log("ok  ", name); } catch (e) { failed++; console.log("FAIL", name, "\n   ", e.message); } };

try {
  // wrong token is rejected
  await t("bad token rejected", async () => {
    const r = await fetch(`http://127.0.0.1:${PORT}/mcp/wrong`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
    assert.equal(r.status, 404);
  });
  const c = new Client({ name: "test", version: "1" });
  await c.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${PORT}/mcp/${TOKEN}`)));
  await t("tools listed", async () => { const { tools } = await c.listTools(); assert.equal(tools.length, 10); });
  await t("status", async () => { const r = await call(c, "server_status"); assert.match(r.t, /20\.9\.3/); assert.match(r.t, /0 active calls/); });
  await t("cli allowed", async () => { const r = await call(c, "cli", { command: "pjsip show endpoints" }); assert.match(r.t, /Endpoint/); });
  await t("cli blocks changes", async () => { const r = await call(c, "cli", { command: "channel request hangup all" }); assert.ok(r.err); });
  await t("cli blocks shell chars", async () => { const r = await call(c, "cli", { command: "core show version; rm -rf /" }); assert.ok(r.err); });
  await t("list configs", async () => { const r = await call(c, "list_configs"); assert.match(r.t, /extensions\.conf/); assert.match(r.t, /pjsip\.conf/); });
  await t("path traversal blocked", async () => { for (const f of ["../../etc/passwd", "/etc/shadow", "x.sh", ".claude-backups/x.conf"]) { const r = await call(c, "read_config", { file: f }); assert.ok(r.err, f); } });
  let sha;
  await t("read config", async () => { const r = await call(c, "read_config", { file: "extensions.conf" }); assert.match(r.t, /Dial\(PJSIP\/100,20\)/); sha = r.t.match(/sha256 ([0-9a-f]{64})/)[1]; });
  await t("edit + diff + reload", async () => {
    const r = await call(c, "edit_config", { file: "extensions.conf", old_text: "Dial(PJSIP/100,20)", new_text: "Dial(PJSIP/100,30)" });
    assert.ok(!r.err, r.t); assert.match(r.t, /-exten => 100,1,Dial\(PJSIP\/100,20\)/); assert.match(r.t, /\+exten => 100,1,Dial\(PJSIP\/100,30\)/);
    assert.match(r.t, /Reloaded dialplan/); assert.match(r.t, /No new warnings/);
  });
  await t("ambiguous edit refused", async () => { const r = await call(c, "edit_config", { file: "extensions.conf", old_text: ",1,Dial(", new_text: ",1,Dial(" }); assert.ok(r.err); assert.match(r.t, /appears 2 times/); });
  await t("stale sha refused", async () => { const r = await call(c, "write_config", { file: "extensions.conf", content: "x", expected_sha256: sha }); assert.ok(r.err); assert.match(r.t, /modified since/); });
  await t("existing file needs sha", async () => { const r = await call(c, "write_config", { file: "pjsip.conf", content: "x" }); assert.ok(r.err); });
  await t("broken edit is rolled back", async () => {
    const r = await call(c, "edit_config", { file: "extensions.conf", old_text: "[from-internal]", new_text: "[from-internal]\nBROKEN" });
    assert.match(r.t, /ERRORS found, so the previous version was restored/);
    const now = await call(c, "read_config", { file: "extensions.conf" }); assert.doesNotMatch(now.t, /BROKEN/); assert.match(now.t, /PJSIP\/100,30/);
  });
  await t("new file", async () => { const r = await call(c, "write_config", { file: "custom/claude_test.conf", content: "; test\n", reload: false }); assert.ok(!r.err, r.t); assert.match(r.t, /new file/); });
  let bk;
  await t("list backups", async () => { const r = await call(c, "list_backups", { file: "extensions.conf" }); bk = r.t.split("\n").at(-1); assert.match(bk, /^extensions\.conf\.\d{8}T\d{6}Z$/); });
  await t("restore oldest backup", async () => {
    const r = await call(c, "restore_backup", { file: "extensions.conf", backup: bk }); assert.ok(!r.err, r.t);
    const now = await call(c, "read_config", { file: "extensions.conf" }); assert.match(now.t, /PJSIP\/100,20/);
  });
  await t("restore refuses other file's backup", async () => { const r = await call(c, "restore_backup", { file: "pjsip.conf", backup: bk }); assert.ok(r.err); });
  await t("tail log filter", async () => { const r = await call(c, "tail_log", { lines: 20, filter: "error" }); assert.match(r.t, /Syntax error/); });
  await t("log name guarded", async () => { const r = await call(c, "tail_log", { log: "../../etc/passwd" }); assert.ok(r.err); });
  await t("reload tool", async () => { const r = await call(c, "reload", { module: "dialplan" }); assert.match(r.t, /Dialplan reloaded/); });
  await t("audit log has no file contents", async () => { assert.ok(logs.join("").includes('"tool":"edit_config"')); assert.ok(!logs.join("").includes("BROKEN")); });
  await c.close();
} finally { srv.kill(); }
console.log(failed ? `\n${failed} failed` : "\nall passed"); process.exit(failed ? 1 : 0);
