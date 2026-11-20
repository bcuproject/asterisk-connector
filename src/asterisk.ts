import { createHash } from "node:crypto";
import { type Runner, shq } from "./runner.js";

export interface AstConfig {
  confDir: string;        // /etc/asterisk
  logDir: string;         // /var/log/asterisk
  asteriskCmd: string;    // e.g. "sudo -n /usr/sbin/asterisk" or "asterisk"
  allowWrite: boolean;
}

export const configFromEnv = (env = process.env): AstConfig => ({
  confDir: (env.ASTERISK_CONF_DIR || "/etc/asterisk").replace(/\/+$/, ""),
  logDir: (env.ASTERISK_LOG_DIR || "/var/log/asterisk").replace(/\/+$/, ""),
  asteriskCmd: env.ASTERISK_CMD || "sudo -n /usr/sbin/asterisk",
  allowWrite: (env.ALLOW_WRITE ?? "true") !== "false",
});

const MAX_FILE = 512 * 1024;
const BACKUPS = ".claude-backups";
const FILE_RE = /^(?:[A-Za-z0-9_-]+\/)?[A-Za-z0-9_.-]+\.(?:conf|ael|lua|txt)$/;
const LOG_RE = /^[A-Za-z0-9_.-]+$/;

export class UserError extends Error {}

export function checkFile(name: string): string {
  const n = String(name || "").trim().replace(/^\/etc\/asterisk\//, "");
  if (!FILE_RE.test(n) || n.includes("..") || n.startsWith(".")) throw new UserError(`"${name}" is not an allowed config file name (e.g. extensions.conf, pjsip.conf, custom/my.conf).`);
  return n;
}

// ---- Asterisk CLI: read-only commands only (plus reloads through their own tool) ----
const READ_CLI: RegExp[] = [
  /^core show (channels?|calls|uptime|version|settings|sysinfo|hints?|codecs?|translation|taskprocessors|threads|config mappings|file formats|image formats|applications?|functions?)\b/,
  /^pjsip (show|list) (endpoints?|aors?|contacts?|registrations?|transports?|auths?|identif(y|ies)|channels?|channelstats|qualify|outbound-publishes?|subscriptions?|settings|version|unidentified|history|scheduled_tasks)\b/,
  /^(sip|iax2) show (peers?|registry|channels?|settings|users?|inuse|objects|netstats|stats)\b/,
  /^dialplan show\b/, /^dialplan debug\b/,
  /^queue show\b/, /^queue rules show\b/,
  /^voicemail show (users|zones|aliases)\b/,
  /^database (show|showkey|get|query)\b/,
  /^module show\b/, /^manager show (connected|users?|commands?|settings)\b/,
  /^confbridge (list|show)\b/, /^features show\b/, /^cdr show status\b/, /^cel show status\b/,
  /^logger show channels\b/, /^rtp show settings\b/, /^stun show status\b/,
  /^moh show (classes|files)\b/, /^agent show\b/, /^bridge show\b/, /^channel show\b/,
  /^acl show\b/, /^http show status\b/, /^odbc show\b/, /^dundi show\b/, /^parking show\b/, /^presencestate list\b/, /^devicestate list\b/,
];
export const RELOADS: Record<string, string> = {
  dialplan: "dialplan reload", pjsip: "module reload res_pjsip.so", sip: "sip reload", iax2: "iax2 reload",
  voicemail: "voicemail reload", queues: "queue reload all", features: "module reload features",
  musiconhold: "moh reload", confbridge: "module reload app_confbridge.so", manager: "manager reload",
  logger: "logger reload", rtp: "module reload res_rtp_asterisk.so", cdr: "module reload cdr", all: "core reload",
};
export function reloadFor(file: string): keyof typeof RELOADS | null {
  const f = file.split("/").pop()!.toLowerCase();
  if (/^extensions|\.ael$|\.lua$/.test(f)) return "dialplan";
  const map: [RegExp, string][] = [[/^pjsip/, "pjsip"], [/^sip/, "sip"], [/^iax/, "iax2"], [/^voicemail/, "voicemail"], [/^queues/, "queues"],
    [/^features/, "features"], [/^musiconhold/, "musiconhold"], [/^confbridge/, "confbridge"], [/^manager/, "manager"], [/^logger/, "logger"],
    [/^rtp/, "rtp"], [/^cdr/, "cdr"]];
  const hit = map.find(([re]) => re.test(f));
  return (hit?.[1] as keyof typeof RELOADS) ?? null;
}

export function checkCli(command: string): string {
  const c = String(command || "").trim().replace(/\s+/g, " ");
  if (!c || c.length > 200 || !/^[\w\s@/.:*+#,=()\-]+$/.test(c)) throw new UserError("CLI command contains characters that aren't allowed.");
  if (!READ_CLI.some((re) => re.test(c.toLowerCase()))) throw new UserError(`"${c}" isn't on the read-only allowlist. Use the reload tool for reloads; changes go through the config tools.`);
  return c;
}

const sha = (s: string) => createHash("sha256").update(s, "utf8").digest("hex");
const stamp = () => new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");

export class Asterisk {
  constructor(private r: Runner, private cfg: AstConfig) {}
  private path(f: string) { return `${this.cfg.confDir}/${f}`; }
  private needWrite() { if (!this.cfg.allowWrite) throw new UserError("This connector is read-only (ALLOW_WRITE=false)."); }

  async cli(command: string, checked = false): Promise<string> {
    const c = checked ? command : checkCli(command);
    const r = await this.r.run(`${this.cfg.asteriskCmd} -rx ${shq(c)}`, undefined, 20_000);
    if (r.code !== 0 && !r.stdout) throw new Error(`asterisk -rx failed (${r.code}): ${r.stderr.trim() || "no output"}`);
    return (r.stdout + (r.stderr ? `\n[stderr] ${r.stderr}` : "")).trim();
  }

  async status(): Promise<string> {
    const parts = await Promise.all(["core show version", "core show uptime", "core show channels count"].map((c) => this.cli(c, true).catch((e) => `(${c}: ${e.message})`)));
    return `Connected via ${this.r.describe()}\n\n${parts.join("\n\n")}`;
  }

  async listConfigs(): Promise<string> {
    const r = await this.r.run(`cd ${shq(this.cfg.confDir)} && find . -maxdepth 2 -type f \\( -name '*.conf' -o -name '*.ael' -o -name '*.lua' \\) -not -path './${BACKUPS}/*' -printf '%TY-%Tm-%Td %TH:%TM  %8s  %P\\n' | sort -k3`);
    if (r.code !== 0) throw new Error(r.stderr.trim() || "could not list config files");
    return r.stdout.trim() || "(no config files found)";
  }

  async read(file: string): Promise<{ content: string; sha256: string }> {
    const f = checkFile(file);
    const r = await this.r.run(`f=${shq(this.path(f))}; [ -f "$f" ] || { echo "No such file: ${f}" >&2; exit 3; }; [ $(stat -c %s "$f") -le ${MAX_FILE} ] || { echo "File too large" >&2; exit 4; }; cat "$f"`);
    if (r.code !== 0) throw new UserError(r.stderr.trim() || `could not read ${f}`);
    return { content: r.stdout, sha256: sha(r.stdout) };
  }

  /** Atomic write with backup. expectSha guards against overwriting a file that changed since it was read. */
  async write(file: string, content: string, expectSha: string | null): Promise<{ backup: string | null; diff: string }> {
    this.needWrite();
    const f = checkFile(file);
    if (Buffer.byteLength(content, "utf8") > MAX_FILE) throw new UserError("Content too large.");
    const ts = stamp(), dir = this.cfg.confDir, bdir = `${dir}/${BACKUPS}`, bname = `${f.replace(/\//g, "__")}.${ts}`;
    const script = [
      `set -e`, `f=${shq(this.path(f))}; b=${shq(`${bdir}/${bname}`)}`,
      `mkdir -p ${shq(bdir)}; chmod 750 ${shq(bdir)} 2>/dev/null || true`,
      expectSha ? `if [ -f "$f" ]; then cur=$(sha256sum "$f" | cut -d' ' -f1); [ "$cur" = ${shq(expectSha)} ] || { echo "CHANGED: $f was modified since it was read; read it again" >&2; exit 9; }; fi` : `true`,
      `if [ -f "$f" ]; then cp -p "$f" "$b"; had=1; else had=0; mkdir -p "$(dirname "$f")"; fi`,
      `tmp=$(mktemp "$f.claude.XXXXXX")`,
      `base64 -d > "$tmp"`,
      `if [ $had = 1 ]; then chmod --reference="$f" "$tmp" 2>/dev/null || true; chgrp --reference="$f" "$tmp" 2>/dev/null || true; else chmod 640 "$tmp"; fi`,
      `mv -f "$tmp" "$f"`,
      `if [ $had = 1 ]; then echo "BACKUP:${bname}"; diff -u --label "${f} (before)" --label "${f} (after)" "$b" "$f" || true; else echo "BACKUP:"; echo "(new file)"; fi`,
    ].join("\n");
    const r = await this.r.run(script, Buffer.from(content, "utf8").toString("base64"));
    if (r.code !== 0) throw new UserError(r.stderr.trim() || `write failed (${r.code})`);
    const [first, ...rest] = r.stdout.split("\n");
    const backup = first.replace(/^BACKUP:/, "") || null;
    return { backup, diff: rest.join("\n").trim() || "(no changes)" };
  }

  async listBackups(file?: string): Promise<string> {
    const prefix = file ? `${checkFile(file).replace(/\//g, "__")}.` : "";
    const r = await this.r.run(`cd ${shq(`${this.cfg.confDir}/${BACKUPS}`)} 2>/dev/null && ls -1t | awk -v p=${shq(prefix)} 'index($0,p)==1' | head -50 || true`);
    return r.stdout.trim() || "(no backups yet)";
  }

  async restore(file: string, backup: string): Promise<{ backup: string | null; diff: string }> {
    this.needWrite();
    const f = checkFile(file);
    if (!/^[A-Za-z0-9_.-]+\.\d{8}T\d{6}Z$/.test(backup) || !backup.startsWith(f.replace(/\//g, "__") + ".")) throw new UserError("Unknown backup name for that file. Use list_backups.");
    const r = await this.r.run(`cat ${shq(`${this.cfg.confDir}/${BACKUPS}/${backup}`)}`);
    if (r.code !== 0) throw new UserError(r.stderr.trim() || "backup not found");
    return this.write(f, r.stdout, null); // restoring also backs up the current version first
  }

  async logLineCount(log = "messages"): Promise<number> {
    const r = await this.r.run(`wc -l < ${shq(`${this.cfg.logDir}/${log}`)} 2>/dev/null || echo 0`);
    return Number(r.stdout.trim()) || 0;
  }

  async logSince(line: number, log = "messages"): Promise<string> {
    const r = await this.r.run(`tail -n +${line + 1} ${shq(`${this.cfg.logDir}/${log}`)} 2>/dev/null | tail -n 200`);
    return r.stdout;
  }

  async tailLog(lines: number, filter?: string, log = "messages"): Promise<string> {
    if (!LOG_RE.test(log)) throw new UserError("Bad log name.");
    const n = Math.max(1, Math.min(1000, Math.floor(lines || 100)));
    const grep = filter ? ` | grep -F -i -- ${shq(filter.slice(0, 120))}` : "";
    const r = await this.r.run(`f=${shq(`${this.cfg.logDir}/${log}`)}; [ -r "$f" ] || { echo "Cannot read $f" >&2; ls -1 ${shq(this.cfg.logDir)} >&2; exit 3; }; tail -n ${filter ? 20000 : n} "$f"${grep} | tail -n ${n}`);
    if (r.code !== 0 && !r.stdout) throw new UserError(r.stderr.trim());
    return r.stdout.trim() || "(no matching lines)";
  }

  /** Reload a module and report any new WARNING/ERROR lines in the log. */
  async reload(module: string): Promise<{ output: string; problems: string[] }> {
    this.needWrite();
    const cmd = RELOADS[module];
    if (!cmd) throw new UserError(`Unknown module "${module}". One of: ${Object.keys(RELOADS).join(", ")}`);
    const before = await this.logLineCount();
    const output = await this.cli(cmd, true);
    await new Promise((r) => setTimeout(r, 1200));
    const fresh = await this.logSince(before);
    const problems = fresh.split("\n").filter((l) => /\b(ERROR|WARNING)\b/.test(l)).slice(-40);
    if (/no such command|unable to|failed/i.test(output)) problems.unshift(`CLI: ${output}`);
    return { output, problems };
  }
}

export function applyEdit(content: string, oldText: string, newText: string, all = false): string {
  if (!oldText) throw new UserError("old_text must not be empty.");
  const count = content.split(oldText).length - 1;
  if (count === 0) throw new UserError("old_text was not found in the file. Read the file again and copy the text exactly (including spaces).");
  if (count > 1 && !all) throw new UserError(`old_text appears ${count} times. Include more surrounding lines so it is unique, or set replace_all.`);
  return all ? content.split(oldText).join(newText) : content.replace(oldText, () => newText);
}
