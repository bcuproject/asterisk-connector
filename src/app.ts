import { type Runner, shq } from "./runner.js";
import { UserError } from "./asterisk.js";

/** App side of the server (e.g. /opt/bcu-livekit and its services), through the root-owned claude-app-helper.
 *  The helper enforces which folders/services are allowed and hides secrets; this class only forwards. */
export class AppServer {
  constructor(private r: Runner, private helper: string, private allowWrite: boolean) {}

  private async call(args: string[], stdin?: string, timeoutMs = 60_000): Promise<string> {
    const res = await this.r.run(`${this.helper} ${args.map(shq).join(" ")}`, stdin, timeoutMs);
    if (res.code !== 0) throw new UserError(res.stderr.trim() || res.stdout.trim() || `helper failed (${res.code})`);
    return res.stdout.trim();
  }
  private needWrite() { if (!this.allowWrite) throw new UserError("This connector is read-only (ALLOW_WRITE=false)."); }

  services() { return this.call(["services"]); }
  logs(unit: string, lines: number, filter?: string) { return this.call(["logs", unit, String(lines), ...(filter ? [filter.slice(0, 120)] : [])]); }
  async restart(unit: string) {
    this.needWrite();
    const j = JSON.parse(await this.call(["restart", unit], undefined, 120_000));
    return `Restarted ${unit}: now ${j.state}${j.restart_exit ? ` (systemctl exit ${j.restart_exit}: ${j.restart_output})` : ""}\n\nLatest log lines:\n${j.recent_logs}`;
  }
  list(folder?: string) { return this.call(["list", ...(folder ? [folder] : [])]); }
  async read(path: string) { return JSON.parse(await this.call(["read", path])) as { path: string; sha256: string; secrets_masked: boolean; content: string }; }
  async edit(path: string, req: { old_text: string; new_text: string; replace_all?: boolean; expected_sha256?: string }) {
    this.needWrite();
    return JSON.parse(await this.call(["edit", path], JSON.stringify(req))) as { backup: string | null; diff: string };
  }
  async write(path: string, content: string, expected_sha256?: string) {
    this.needWrite();
    return JSON.parse(await this.call(["write", path], JSON.stringify({ content, expected_sha256 }))) as { backup: string | null; diff: string };
  }
  backups(path?: string) { return this.call(["backups", ...(path ? [path] : [])]); }
  async restore(path: string, backup: string) {
    this.needWrite();
    return JSON.parse(await this.call(["restore", path, backup])) as { backup: string | null; diff: string };
  }
}
