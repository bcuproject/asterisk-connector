import { spawn } from "node:child_process";
import express from "express";
import { CLOUD_UI } from "./cloud-ui.js";
import { type Runner, shq } from "./runner.js";
import { UserError } from "./asterisk.js";

/** Creating the Sweet Spot server on DigitalOcean, through the root-owned claude-cloud-helper.
 *  The helper holds the DigitalOcean token (this process never sees it) and only knows how to create ONE
 *  Sweet Spot server: no delete, resize or power actions exist anywhere in this path. */
export class CloudServer {
  constructor(private r: Runner, private helper: string, private allowWrite: boolean) {}

  private async call(args: string[], stdin?: string, timeoutMs = 60_000): Promise<string> {
    const res = await this.r.run(`${this.helper} ${args.map(shq).join(" ")}`, stdin, timeoutMs);
    if (res.code !== 0) throw new UserError(res.stderr.trim() || res.stdout.trim() || `cloud helper failed (${res.code})`);
    return res.stdout.trim();
  }

  status() { return this.call(["status"]); }
  sshKeys() { return this.call(["ssh-keys"]); }
  create(customDomain: string | undefined) {
    if (!this.allowWrite) throw new UserError("This connector is read-only (ALLOW_WRITE=false).");
    return this.call(["create"], JSON.stringify({ confirm: true, custom_domain: customDomain ?? "" }), 90_000);
  }

  /** Public routes the NEW server uses while it installs itself. Each needs its own one-time code (checked by the helper). */
  routes(app: express.Express) {
    app.get("/cloud/kit/:code", (req, res) => {
      const code = String(req.params.code ?? "");
      if (!/^[0-9a-f]{48}$/.test(code)) { res.status(404).end(); return; }
      // Streamed straight from the helper: the kit is bigger than the normal command output limit.
      const p = spawn("/bin/sh", ["-c", `${this.helper} kit ${shq(code)}`], { stdio: ["ignore", "pipe", "ignore"] });
      let started = false;
      p.stdout.on("data", (d) => { if (!started) { started = true; res.status(200).type("text/plain"); } res.write(d); });
      p.on("close", (exit) => { if (!started) res.status(404); if (exit !== 0 && started) res.destroy(); else res.end(); });
      const t = setTimeout(() => p.kill("SIGKILL"), 120_000);
      p.on("close", () => clearTimeout(t));
    });
    // The private page (its key is checked by the helper on every action; 10 wrong keys lock it for 15 minutes).
    app.get("/cloud/ui", (_req, res) => {
      res.set({ "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store" }).type("html").send(CLOUD_UI);
    });
    const refused = async (res: express.Response, err: string) => {
      const locked = /wrong key|locked/.test(err);
      if (locked) await new Promise((r) => setTimeout(r, 500));
      res.status(locked ? 403 : 400).json({ error: locked ? (err.includes("locked") ? "Too many wrong keys. Try again in 15 minutes." : "This link isn't valid.") : err.slice(0, 300) });
    };
    app.post("/cloud/api/kit", express.raw({ type: "*/*", limit: "26mb" }), (req, res) => {
      if (!this.allowWrite) { res.status(400).json({ error: "This connector is read-only." }); return; }
      const key = String(req.headers["x-ui-key"] ?? "");
      const body = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
      // Binary-safe: the zip goes to the helper's stdin directly (after one JSON line with the key).
      const p = spawn("/bin/sh", ["-c", `${this.helper} ui-kit`], { stdio: ["pipe", "pipe", "pipe"] });
      let out = "", err = "";
      p.stdout.on("data", (d) => { out += d; });
      p.stderr.on("data", (d) => { if (err.length < 2000) err += d; });
      p.stdin.on("error", () => {});
      p.on("close", async (code) => { if (code === 0) res.json(JSON.parse(out || "{}")); else await refused(res, err.trim() || "upload failed"); });
      p.stdin.write(JSON.stringify({ key }) + "\n");
      p.stdin.end(body);
    });
    app.post("/cloud/api/:what", async (req, res) => {
      const what = String(req.params.what);
      if (!["status", "token", "create"].includes(what)) { res.status(404).end(); return; }
      if (what !== "status" && !this.allowWrite) { res.status(400).json({ error: "This connector is read-only." }); return; }
      const out = await this.r.run(`${this.helper} ui ${what}`, JSON.stringify(req.body ?? {}), 90_000);
      if (out.code === 0) res.type("json").send(out.stdout); else await refused(res, out.stderr.trim() || "failed");
    });
    app.post("/cloud/callback", async (req, res) => {
      const body = JSON.stringify(req.body ?? {});
      const out = await this.r.run(`${this.helper} callback`, body, 30_000);
      res.status(out.code === 0 ? 200 : 404).json(out.code === 0 ? { ok: true } : { error: "not found" });
    });
  }
}
