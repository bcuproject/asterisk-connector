import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { Client, type ConnectConfig } from "ssh2";

export interface RunResult { code: number; stdout: string; stderr: string }
export interface Runner { run(cmd: string, stdin?: string, timeoutMs?: number): Promise<RunResult>; describe(): string }

const MAX_OUT = 2_000_000;

/** Single-quote a value for POSIX sh. */
export const shq = (s: string) => `'${String(s).replace(/'/g, `'\\''`)}'`;

/** Runs commands on the same machine (use when the connector is installed on the Asterisk server itself). */
export class LocalRunner implements Runner {
  describe() { return "local"; }
  run(cmd: string, stdin?: string, timeoutMs = 30_000): Promise<RunResult> {
    return new Promise((resolve) => {
      const p = spawn("/bin/sh", ["-c", cmd], { stdio: ["pipe", "pipe", "pipe"] });
      let stdout = "", stderr = "";
      const t = setTimeout(() => p.kill("SIGKILL"), timeoutMs);
      p.stdout.on("data", (d) => { if (stdout.length < MAX_OUT) stdout += d; });
      p.stderr.on("data", (d) => { if (stderr.length < MAX_OUT) stderr += d; });
      p.on("close", (code) => { clearTimeout(t); resolve({ code: code ?? -1, stdout, stderr }); });
      p.stdin.on("error", () => {}); // command may exit without reading stdin
      p.on("error", (e) => { clearTimeout(t); resolve({ code: -1, stdout, stderr: String(e) }); });
      p.stdin.end(stdin ?? "");
    });
  }
}

/** Runs commands on the Asterisk server over SSH, with a pinned host key. */
export class SshRunner implements Runner {
  private conn: Client | null = null;
  private connecting: Promise<Client> | null = null;
  constructor(private cfg: { host: string; port: number; username: string; privateKey: string; passphrase?: string; fingerprint: string }) {}
  describe() { return `ssh ${this.cfg.username}@${this.cfg.host}:${this.cfg.port}`; }

  static fromEnv(env = process.env): SshRunner {
    const keyRaw = env.ASTERISK_SSH_KEY ?? "";
    const privateKey = keyRaw.includes("PRIVATE KEY") ? keyRaw.replace(/\\n/g, "\n") : keyRaw ? readFileSync(keyRaw, "utf8") : "";
    if (!env.ASTERISK_SSH_HOST || !privateKey) throw new Error("Set ASTERISK_SSH_HOST and ASTERISK_SSH_KEY (key contents or a file path).");
    return new SshRunner({
      host: env.ASTERISK_SSH_HOST, port: Number(env.ASTERISK_SSH_PORT || 22), username: env.ASTERISK_SSH_USER || "claude-mcp",
      privateKey, passphrase: env.ASTERISK_SSH_KEY_PASSPHRASE || undefined, fingerprint: (env.ASTERISK_HOST_FINGERPRINT || "").trim(),
    });
  }

  private connect(): Promise<Client> {
    if (this.conn) return Promise.resolve(this.conn);
    if (this.connecting) return this.connecting;
    this.connecting = new Promise<Client>((resolve, reject) => {
      const c = new Client();
      let seen = "";
      const want = this.cfg.fingerprint.replace(/^SHA256:/, "").replace(/=+$/, "");
      const opts: ConnectConfig = {
        host: this.cfg.host, port: this.cfg.port, username: this.cfg.username, privateKey: this.cfg.privateKey, passphrase: this.cfg.passphrase,
        readyTimeout: 15_000, keepaliveInterval: 20_000,
        hostVerifier: (key: Buffer) => {
          seen = createHash("sha256").update(key).digest("base64").replace(/=+$/, "");
          return !!want && seen === want;
        },
      };
      c.on("ready", () => { this.conn = c; this.connecting = null; resolve(c); });
      c.on("error", (e) => {
        this.conn = null; this.connecting = null;
        if (seen && seen !== want) reject(new Error(`Server host key SHA256:${seen} does not match ASTERISK_HOST_FINGERPRINT. ${want ? "Refusing to connect." : "Set ASTERISK_HOST_FINGERPRINT to this value if it is your server."}`));
        else reject(e);
      });
      c.on("close", () => { this.conn = null; });
      c.connect(opts);
    });
    return this.connecting;
  }

  async run(cmd: string, stdin?: string, timeoutMs = 30_000): Promise<RunResult> {
    const c = await this.connect();
    return new Promise((resolve, reject) => {
      c.exec(cmd, (err, stream) => {
        if (err) { this.conn = null; return reject(err); }
        let stdout = "", stderr = "";
        const t = setTimeout(() => { stream.close(); }, timeoutMs);
        stream.on("data", (d: Buffer) => { if (stdout.length < MAX_OUT) stdout += d; });
        stream.stderr.on("data", (d: Buffer) => { if (stderr.length < MAX_OUT) stderr += d; });
        let exitCode: number | null = null;
        stream.on("exit", (code: number | null) => { exitCode = code; });
        stream.on("close", (code: number | null) => { clearTimeout(t); resolve({ code: exitCode ?? code ?? -1, stdout, stderr }); });
        stream.on("error", () => {});
        stream.end(stdin ?? "");
      });
    });
  }
}
