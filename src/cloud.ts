import { spawn } from "node:child_process";
import type express from "express";
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
    app.post("/cloud/callback", async (req, res) => {
      const body = JSON.stringify(req.body ?? {});
      const out = await this.r.run(`${this.helper} callback`, body, 30_000);
      res.status(out.code === 0 ? 200 : 404).json(out.code === 0 ? { ok: true } : { error: "not found" });
    });
  }
}
