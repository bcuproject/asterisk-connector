#!/usr/bin/env python3
"""claude-app-helper: the only root-level door the connector has into the app side of the server.

Installed root-owned at /usr/local/sbin/claude-app-helper; the connector's user may run it via sudo.
All safety rules live HERE (not in the connector), so they hold even if the connector is compromised:
  * files: only inside the folders listed in /etc/claude-app-helper.conf (root-owned)
  * never: .env files, keys/certificates, call recordings, .git, virtualenvs
  * secrets in readable files are masked; a file containing secrets can't be rewritten by Claude
  * services: only the units listed in the conf; actions: status, logs, restart
  * every write keeps a backup; Python files must compile before they are saved
"""
import difflib, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

CONF = "/etc/claude-app-helper.conf"
if os.geteuid() != 0 and os.environ.get("CLAUDE_APP_HELPER_CONF"):  # tests only; root never trusts env
    CONF = os.environ["CLAUDE_APP_HELPER_CONF"]

MAX = 1024 * 1024
DENY_PARTS = {".git", "node_modules", "__pycache__", "venv", ".venv", "recordings", ".claude-backups"}
DENY_NAME = re.compile(r"(^\.env|\.env$|\.env\.|\.pem$|\.key$|\.p12$|\.pfx$|\.crt$|^id_(rsa|ed25519|ecdsa)|secret|credential|\.sqlite3?$|\.db$|\.wav$|\.ogg$|\.mp3$|\.opus$)", re.I)
TEXT_EXT = re.compile(r"\.(py|ts|js|mjs|cjs|json|ya?ml|toml|ini|cfg|conf|txt|md|sh|service|sql|csv|html|css)$|^(Dockerfile|Makefile|requirements[\w.-]*)$", re.I)
_NAME = r"(?:api[_-]?key|apikey|secret|token|password|passwd|private[_-]?key|access[_-]?key|client[_-]?secret|auth[_-]?token|bearer)[\w-]*"
SECRET_PATTERNS = [
    # name = "literal" / "name": "literal"  (code, JSON, YAML)
    re.compile(r"(?i)(\b" + _NAME + r"[\"']?\s*[:=]\s*)([\"'])([^\"'\s]{8,})\2"),
    # env / ini style at line start: FISH_AUDIO_API_KEY=xyz
    re.compile(r"(?m)(^\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*\s*=\s*)([\"']?)([^\s#\"']{6,})\2"),
    # well-known key formats anywhere
    re.compile(r"()()(sk-[A-Za-z0-9_-]{16,}|sk_live_[A-Za-z0-9]{8,}|sup_sk_[A-Za-z0-9]{8,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN[ A-Z]*PRIVATE KEY-----)"),
]
# Logs also mask loose key=value pairs (e.g. URLs, debug prints); not used for code, where it would hide normal code.
LOG_PATTERNS = SECRET_PATTERNS + [re.compile(r"(?i)(\b(?:token|key|secret|password|pass|sig|signature|authorization)[=:]\s*)()((?:Bearer\s+)?[^\s&\"',}]{8,})")]


def die(msg, code=2):
    print(msg, file=sys.stderr); sys.exit(code)


def conf():
    try:
        st = os.stat(CONF)
    except FileNotFoundError:
        die(f"{CONF} missing")
    if os.geteuid() == 0 and (st.st_uid != 0 or st.st_mode & 0o022):
        die(f"{CONF} must be owned by root and not writable by others")
    c = {"dirs": [], "units": [], "backup_dir": "/var/backups/claude-app", "systemctl": "systemctl", "journalctl": "journalctl", "python": sys.executable}
    for line in open(CONF):
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        if k in ("dirs", "units"):
            c[k] = [x.strip() for x in v.split(",") if x.strip()]
        elif k in c:
            c[k] = v
    c["dirs"] = [os.path.realpath(d) for d in c["dirs"] if os.path.isdir(d)]
    return c


def mask(text, logs=False):
    for rx in (LOG_PATTERNS if logs else SECRET_PATTERNS):
        text = rx.sub(lambda m: m.group(1) + m.group(2) + "«hidden»" + m.group(2), text)
    return text


def has_secret(text):
    return any(rx.search(text) for rx in SECRET_PATTERNS)


def resolve(c, p):
    if not p or "\x00" in p:
        die("bad path")
    return os.path.realpath(p if os.path.isabs(p) else os.path.join(c["dirs"][0] if c["dirs"] else "/", p))


def safe_path(c, p, must_exist=True):
    full = resolve(c, p)
    root = next((d for d in c["dirs"] if full == d or full.startswith(d + os.sep)), None)
    if not root:
        die(f"{p} is outside the allowed folders: {', '.join(c['dirs']) or '(none)'}")
    rel = os.path.relpath(full, root)
    parts = rel.split(os.sep)
    if any(x in DENY_PARTS for x in parts) or DENY_NAME.search(parts[-1]):
        die(f"{p} is a protected file (keys, .env, recordings and similar are never readable)")
    if must_exist and not os.path.isfile(full):
        die(f"No such file: {p}")
    if not must_exist and not os.path.isfile(full) and not TEXT_EXT.search(parts[-1]):
        die("Only text/code files can be created")
    return full


def sha(b):
    return hashlib.sha256(b).hexdigest()


def read_raw(full):
    if os.path.getsize(full) > MAX:
        die("File too large (over 1 MB)")
    data = open(full, "rb").read()
    if b"\x00" in data[:4096]:
        die("Binary file; not readable here")
    return data


def cmd_list(c, sub=""):
    bases = [safe_path_dir(c, resolve(c, sub))] if sub else c["dirs"]
    out = []
    for base in bases:
        for dp, dns, fns in os.walk(base):
            dns[:] = sorted(d for d in dns if d not in DENY_PARTS and not d.startswith("."))
            if dp.count(os.sep) - base.count(os.sep) >= 3:
                dns[:] = []
            for f in sorted(fns):
                if DENY_NAME.search(f):
                    continue
                p = os.path.join(dp, f)
                st = os.stat(p)
                out.append(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}  {st.st_size:>8}  {p}")
            if len(out) > 400:
                out.append("… (truncated)"); break
    print("\n".join(out) or "(no files)")


def safe_path_dir(c, p):
    full = os.path.realpath(p)
    if not any(full == d or full.startswith(d + os.sep) for d in c["dirs"]) or any(x in DENY_PARTS for x in full.split(os.sep)):
        die(f"{p} is not an allowed folder")
    return full


def cmd_read(c, p):
    full = safe_path(c, p)
    data = read_raw(full)
    text = data.decode("utf-8", "replace")
    print(json.dumps({"path": full, "sha256": sha(data), "secrets_masked": has_secret(text), "content": mask(text)}))


def backup(c, full):
    os.makedirs(c["backup_dir"], mode=0o700, exist_ok=True)
    name = full.strip("/").replace("/", "__") + "." + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    shutil.copy2(full, os.path.join(c["backup_dir"], name))
    return name


def check_python(c, full, data):
    if not full.endswith(".py"):
        return
    # Compile with the service's own Python (no .pyc written); a syntax error never reaches the live file.
    r = subprocess.run([c["python"], "-c", "import sys; compile(sys.stdin.buffer.read(), sys.argv[1], 'exec')", os.path.basename(full)],
                       input=data, capture_output=True, timeout=30)
    if r.returncode:
        die("Not saved: the Python file would not compile:\n" + r.stderr.decode("utf-8", "replace").strip()[-1500:], 5)


def save(c, full, new, old):
    check_python(c, full, new)
    b = backup(c, full) if old is not None else None
    d = os.path.dirname(full)
    fd, tmp = tempfile.mkstemp(prefix=".claude.", dir=d)
    with os.fdopen(fd, "wb") as f:
        f.write(new)
    if old is not None:
        st = os.stat(full); os.chmod(tmp, st.st_mode & 0o7777); os.chown(tmp, st.st_uid, st.st_gid) if os.geteuid() == 0 else None
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, full)
    diff = "".join(difflib.unified_diff((old or b"").decode("utf-8", "replace").splitlines(True), new.decode("utf-8", "replace").splitlines(True),
                                        f"{full} (before)", f"{full} (after)")) if old is not None else "(new file)"
    print(json.dumps({"backup": b, "diff": mask(diff)[:60000]}))


def cmd_edit(c, p):
    req = json.load(sys.stdin)
    full = safe_path(c, p)
    data = read_raw(full)
    text = data.decode("utf-8")
    if req.get("expected_sha256") and req["expected_sha256"] != sha(data):
        die("CHANGED: the file was modified since it was read; read it again", 9)
    old, new = req.get("old_text", ""), req.get("new_text", "")
    if not old:
        die("old_text must not be empty")
    if "«hidden»" in old or "«hidden»" in new:
        die("Edits can't touch masked secret values; change those by hand on the server")
    n = text.count(old)
    if n == 0:
        die("old_text was not found. Read the file again and copy the text exactly (including spaces).")
    if n > 1 and not req.get("replace_all"):
        die(f"old_text appears {n} times. Include more surrounding lines so it is unique, or set replace_all.")
    out = text.replace(old, new) if req.get("replace_all") else text.replace(old, new, 1)
    if has_secret(new):
        die("New text looks like it contains a secret; put secrets in the service's environment file instead")
    save(c, full, out.encode("utf-8"), data)


def cmd_write(c, p):
    req = json.load(sys.stdin)
    full = safe_path(c, p, must_exist=False)
    exists = os.path.isfile(full)
    content = req.get("content", "").encode("utf-8")
    if len(content) > MAX:
        die("Content too large")
    if has_secret(req.get("content", "")):
        die("Content looks like it contains a secret; put secrets in the service's environment file instead")
    old = None
    if exists:
        old = read_raw(full)
        if has_secret(old.decode("utf-8", "replace")):
            die("This file contains secrets, so it can only be changed with edit (which keeps them), not replaced whole")
        if req.get("expected_sha256") != sha(old):
            die("CHANGED or missing expected_sha256: read the file first and pass its sha256", 9)
    else:
        os.makedirs(os.path.dirname(full), exist_ok=True)
    save(c, full, content, old)


def cmd_backups(c, p=""):
    bd = c["backup_dir"]
    prefix = resolve(c, p).strip("/").replace("/", "__") + "." if p else ""
    names = sorted((n for n in (os.listdir(bd) if os.path.isdir(bd) else []) if n.startswith(prefix)),
                   key=lambda n: os.path.getmtime(os.path.join(bd, n)), reverse=True)
    print("\n".join(names[:50]) or "(no backups yet)")


def cmd_restore(c, p, name):
    full = safe_path(c, p)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.\d{8}T\d{6}Z", name) or not name.startswith(full.strip("/").replace("/", "__") + "."):
        die("Unknown backup name for that file. Use backups.")
    src = os.path.join(c["backup_dir"], name)
    if not os.path.isfile(src):
        die("Backup not found")
    save(c, full, open(src, "rb").read(), read_raw(full))


def unit_ok(c, u):
    if u not in c["units"]:
        die(f"{u} is not an allowed service. Allowed: {', '.join(c['units']) or '(none)'}")
    return u


def run(args, timeout=30):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return mask((r.stdout + r.stderr).strip(), logs=True)


def cmd_services(c):
    rows = []
    for u in c["units"]:
        st = run([c["systemctl"], "show", u, "-p", "ActiveState", "-p", "SubState", "-p", "ActiveEnterTimestamp", "-p", "ExecStart", "-p", "WorkingDirectory", "--no-pager"])
        rows.append(f"== {u}\n{st}")
    print("\n\n".join(rows) or "(no services configured)")


def cmd_logs(c, u, n="100", flt=""):
    unit_ok(c, u)
    n = str(max(1, min(2000, int(n))))
    out = run([c["journalctl"], "-u", u, "-n", "20000" if flt else n, "--no-pager", "-o", "short-iso"], 60)
    lines = out.splitlines()
    if flt:
        lines = [l for l in lines if flt.lower() in l.lower()]
    print("\n".join(lines[-int(n):]) or "(no matching lines)")


def cmd_restart(c, u):
    unit_ok(c, u)
    r = subprocess.run([c["systemctl"], "restart", u], capture_output=True, text=True, timeout=90)
    time.sleep(4)
    state = run([c["systemctl"], "is-active", u])
    print(json.dumps({"restart_exit": r.returncode, "restart_output": mask((r.stdout + r.stderr).strip(), logs=True), "state": state,
                      "recent_logs": run([c["journalctl"], "-u", u, "-n", "25", "--no-pager", "-o", "short-iso"])}))


def main():
    c = conf()
    a = sys.argv[1:]
    cmds = {"list": (cmd_list, 0, 1), "read": (cmd_read, 1, 1), "edit": (cmd_edit, 1, 1), "write": (cmd_write, 1, 1),
            "backups": (cmd_backups, 0, 1), "restore": (cmd_restore, 2, 2), "services": (cmd_services, 0, 0),
            "logs": (cmd_logs, 1, 3), "restart": (cmd_restart, 1, 1), "dirs": (lambda c: print("\n".join(c["dirs"])), 0, 0)}
    if not a or a[0] not in cmds:
        die("usage: claude-app-helper " + "|".join(cmds))
    fn, lo, hi = cmds[a[0]]
    if not lo <= len(a) - 1 <= hi:
        die(f"{a[0]}: wrong number of arguments")
    fn(c, *a[1:])


if __name__ == "__main__":
    main()
