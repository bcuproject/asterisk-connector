// End-to-end test of the Sweet Spot server tools through the connector: node test/cloud-e2e.mjs
// The helper runs with test folders and a canned DigitalOcean (test/cloud-helper-stub.py).
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { createHash, randomBytes } from "node:crypto";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const D = "/tmp/fakecloud"; rmSync(D, { recursive: true, force: true }); mkdirSync(`${D}/state`, { recursive: true });
writeFileSync(`${D}/cloud.env`, "DO_TOKEN=dop_v1_secret\nPUBLIC_HOST=bcu.example.com\n");
const kit = randomBytes(2_500_000);  // bigger than the normal 2 MB command output limit
writeFileSync(`${D}/state/sweetspot-system.zip`, kit);
const TOKEN = "k".repeat(40), PORT = 18789;
const env = { ...process.env, CONNECTOR_TOKEN: TOKEN, PORT: String(PORT), RUN_MODE: "local", ASTERISK_TOOLS: "false",
  CLOUD_HELPER_CMD: `env CLOUD_TEST_DIR=${D} python3 ${process.cwd()}/test/cloud-helper-stub.py` };
const srv = spawn("node", ["dist/server.js"], { env, stdio: ["ignore", "pipe", "inherit"] });
const logs = []; srv.stdout.on("data", (d) => logs.push(String(d)));
await new Promise((r) => setTimeout(r, 800));
const call = async (c, name, args = {}) => { const r = await c.callTool({ name, arguments: args }); return { err: !!r.isError, t: r.content.map((x) => x.text).join("\n") }; };
let failed = 0; const t = async (name, fn) => { try { await fn(); console.log("ok  ", name); } catch (e) { failed++; console.log("FAIL", name, "\n   ", e.message); } };
const base = `http://127.0.0.1:${PORT}`;
try {
  const c = new Client({ name: "test", version: "1" });
  await c.connect(new StreamableHTTPClientTransport(new URL(`${base}/mcp/${TOKEN}`)));
  await t("cloud tools listed (and nothing to delete servers)", async () => {
    const names = (await c.listTools()).tools.map((x) => x.name);
    assert.deepEqual(names.sort(), ["cloud_ssh_keys", "cloud_status", "create_sweetspot_server"]);
  });
  await t("status shows token set and kit uploaded, token itself never", async () => {
    const r = await call(c, "cloud_status"); assert.match(r.t, /"token_set": true/); assert.match(r.t, /"bytes": 2500000/); assert.doesNotMatch(r.t, /dop_v1/);
  });
  await t("create needs confirm: true", async () => { const r = await call(c, "create_sweetspot_server", {}); assert.equal(r.err, true); });
  let ud = "";
  await t("create makes the server", async () => {
    const r = await call(c, "create_sweetspot_server", { confirm: true }); assert.equal(r.err, false, r.t); assert.match(r.t, /"droplet_id": 77/);
    ud = readFileSync(`${D}/user_data`, "utf8");
  });
  await t("second create refused", async () => { const r = await call(c, "create_sweetspot_server", { confirm: true }); assert.equal(r.err, true); assert.match(r.t, /already exists/); });
  const kitCode = ud.match(/\/cloud\/kit\/([0-9a-f]{48})/)[1], cb = ud.match(/CALLBACK_TOKEN='([0-9a-f]{48})'/)[1];
  await t("kit route refuses a wrong code", async () => { const r = await fetch(`${base}/cloud/kit/${"0".repeat(48)}`); assert.equal(r.status, 404); });
  await t("kit route streams the whole kit (2.5 MB) with the right code", async () => {
    const r = await fetch(`${base}/cloud/kit/${kitCode}`); assert.equal(r.status, 200);
    const got = Buffer.from(await r.text(), "base64");
    assert.equal(createHash("sha256").update(got).digest("hex"), createHash("sha256").update(kit).digest("hex"));
  });
  await t("callback refuses a wrong code", async () => {
    const r = await fetch(`${base}/cloud/callback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: "0".repeat(48), ok: true }) });
    assert.equal(r.status, 404);
  });
  await t("callback with the right code marks the server ready", async () => {
    const dom = "9-9-9-9.sslip.io";
    const r = await fetch(`${base}/cloud/callback`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cb, ok: true, ip: "9.9.9.9", domain: dom, setup_url: `https://${dom}/setup#t=${"e".repeat(48)}` }) });
    assert.equal(r.status, 200);
    const s = await call(c, "cloud_status"); assert.match(s.t, /"status": "ready"/); assert.match(s.t, /setup#t=e{48}/);
  });
  await t("MCP needs the connector code", async () => { const r = await fetch(`${base}/mcp/wrong`, { method: "POST" }); assert.equal(r.status, 404); });
  await t("audit log never holds the token or codes", async () => { const l = logs.join(""); assert.doesNotMatch(l, /dop_v1_secret/); assert.ok(!l.includes(kitCode) && !l.includes(cb)); });
  await c.close();
} finally { srv.kill(); }
console.log(failed ? `\n${failed} FAILED` : "\nAll cloud connector checks passed.");
process.exit(failed ? 1 : 0);
