// Minimal SSH server (ssh2) used only by tests: accepts one public key and runs exec requests with /bin/sh.
import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { timingSafeEqual } from "node:crypto";
import ssh2 from "ssh2";
const { Server, utils } = ssh2;
const [hostKey, pubFile, port] = process.argv.slice(2);
const allowed = utils.parseKey(readFileSync(pubFile));
new Server({ hostKeys: [readFileSync(hostKey)] }, (client) => {
  client.on("authentication", (ctx) => {
    if (ctx.method === "publickey" && ctx.key.algo === allowed.type && timingSafeEqual(ctx.key.data, allowed.getPublicSSH())
      && (!ctx.signature || allowed.verify(ctx.blob, ctx.signature, ctx.hashAlgo) === true)) return ctx.accept();
    ctx.reject(["publickey"]);
  }).on("ready", () => client.on("session", (accept) => accept().on("exec", (acc, _rej, info) => {
    const s = acc(); const p = spawn("/bin/sh", ["-c", info.command]);
    s.pipe(p.stdin).on("error", () => {}); p.stdin.on("error", () => {}); p.stdout.pipe(s, { end: false }); p.stderr.pipe(s.stderr, { end: false });
    p.on("close", (code) => { s.exit(code ?? 1); s.end(); });
  }))).on("error", () => {});
}).listen(Number(port), "127.0.0.1", () => console.log("ssh test server on", port));
