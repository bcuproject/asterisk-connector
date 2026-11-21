#!/usr/bin/env python3
"""claude-cloud-helper: lets the connector create ONE Sweet Spot server on DigitalOcean, and nothing else.

Root-owned. The connector's service user may run it through sudo; the DigitalOcean token lives in
/etc/claude-cloud.env (root-only), so the connector itself never sees it.

  status                     what exists: token set?, kit uploaded?, Sweet Spot droplets, install progress
  ssh-keys                   the account's SSH key names (which go on the new server)
  create      (stdin JSON)   {"confirm": true, "custom_domain": ""}  creates the droplet (refuses if one exists)
  kit <token>                base64 of the kit, for the new server only (one-time code, 3 hours, 5 downloads)
  callback    (stdin JSON)   the new server's report when installing finishes (one-time code)
  set-token                  (admin, interactive) store the DigitalOcean token with a hidden prompt
  connector-url              (admin) print the new server's connector address, to add in Claude settings
  ui <status|token|create>   (stdin JSON with "key") the private web page's actions; the key is UI_KEY in /etc/claude-cloud.env
  ui-kit                     (stdin: one JSON line with "key", then the zip bytes) the page's kit upload
  ui-link                    (admin) print the private page's address

Deliberately missing: delete, resize, power off, rebuild, or any DigitalOcean call other than
GET /v2/account/keys, GET /v2/droplets, GET /v2/droplets/<id> and POST /v2/droplets."""
import base64
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

ENV = "/etc/claude-cloud.env"
STATE_DIR = "/var/lib/claude-cloud"
KIT = os.path.join(STATE_DIR, "sweetspot-system.zip")
STATE = os.path.join(STATE_DIR, "state.json")
CONNECTOR_URL = os.path.join(STATE_DIR, "new-server-connector.txt")
TAG = "sweetspot-orders"
DROPLET = {"name": "sweetspot-orders", "region": "lon1", "size": "s-1vcpu-2gb", "image": "ubuntu-24-04-x64",
           "tags": [TAG], "monitoring": True, "ipv6": False}
CODE_HOURS = 3
KIT_DOWNLOADS = 5
API = "https://api.digitalocean.com/v2"


class Refused(Exception):
    pass


def env() -> dict:
    out = {}
    try:
        with open(ENV) as f:
            for line in f:
                m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
                if m:
                    out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def write_env(values: dict) -> None:
    lines = [f"{k}={v}" for k, v in values.items()]
    tmp = ENV + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# DigitalOcean access for claude-cloud-helper. Root-only. Never paste this token into chat.\n" + "\n".join(lines) + "\n")
    os.replace(tmp, ENV)


def load_state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(s: dict) -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = STATE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def do(method: str, path: str, body: dict | None = None) -> dict:
    base = path.split("?", 1)[0]
    allowed = (method == "GET" and (base == "/account/keys" or re.fullmatch(r"/droplets(/\d+)?", base) is not None)) or (method == "POST" and base == "/droplets")
    if not allowed:
        raise Refused("That DigitalOcean action isn't allowed.")
    token = env().get("DO_TOKEN", "")
    if not token:
        raise Refused("No DigitalOcean token yet. On the BCU server run: sudo claude-cloud-helper set-token")
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", "")
        except ValueError:
            msg = ""
        raise Refused(f"DigitalOcean refused ({e.code}): {msg}"[:200].replace(token, "«hidden»"))
    except urllib.error.URLError as e:
        raise Refused(f"Couldn't reach DigitalOcean: {e.reason}")


def droplets() -> list:
    d = do("GET", f"/droplets?tag_name={TAG}&per_page=20").get("droplets", [])
    out = []
    for x in d:
        ip = next((n["ip_address"] for n in x.get("networks", {}).get("v4", []) if n.get("type") == "public"), None)
        out.append({"id": x["id"], "name": x["name"], "status": x["status"], "ip": ip, "region": x.get("region", {}).get("slug"),
                    "size": x.get("size_slug"), "created_at": x.get("created_at")})
    return out


def kit_info():
    try:
        st = os.stat(KIT)
    except OSError:
        return None
    h = hashlib.sha256()
    with open(KIT, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"bytes": st.st_size, "sha256": h.hexdigest(), "uploaded": time.strftime("%Y-%m-%d %H:%M", time.gmtime(st.st_mtime))}


def cmd_status(_):
    e, s = env(), load_state()
    info = {"token_set": bool(e.get("DO_TOKEN")), "public_host": e.get("PUBLIC_HOST") or None, "kit": kit_info(), "droplets": None,
            "install": {k: s.get(k) for k in ("status", "droplet_id", "started_at", "finished_at", "domain", "ip", "setup_url", "error", "log_tail") if s.get(k)}}
    info["install"]["new_server_connector"] = "ready (address: sudo claude-cloud-helper connector-url, on the BCU server)" if os.path.exists(CONNECTOR_URL) else None
    if info["token_set"]:
        try:
            info["droplets"] = droplets()
        except Refused as ex:
            info["droplets_error"] = str(ex)
    if not info["kit"]:
        info["next_step"] = f"Upload the kit: scp sweetspot-system.zip root@<BCU server>:{KIT}"
    elif not info["token_set"]:
        info["next_step"] = "Store the DigitalOcean token on the BCU server: sudo claude-cloud-helper set-token"
    return info


def cmd_ssh_keys(_):
    return [{"id": k["id"], "name": k["name"]} for k in do("GET", "/account/keys?per_page=50").get("ssh_keys", [])]


USER_DATA = r"""#!/bin/bash
# Sweet Spot server: installs itself once, then reports back. Written by claude-cloud-helper.
set -uo pipefail
exec > /var/log/sweetspot-provision.log 2>&1
KIT_URL='__KIT_URL__'
KIT_SHA256='__KIT_SHA256__'
CALLBACK_URL='__CALLBACK_URL__'
CALLBACK_TOKEN='__CALLBACK_TOKEN__'
CUSTOM_DOMAIN='__CUSTOM_DOMAIN__'
CONNECTOR_TAR='https://github.com/bcuproject/asterisk-connector/archive/refs/heads/main.tar.gz'

report() {  # $1 = true/false
  local tail curl_body cu=""
  tail=$(tail -n 30 /var/log/sweetspot-provision.log | tr -d '\000-\010\013-\037"\\' | tail -c 1500)
  [ -f /etc/asterisk-connector.env ] && cu="https://$DOMAIN/mcp/$(sed -n 's/^CONNECTOR_TOKEN=//p' /etc/asterisk-connector.env)"
  curl_body="{\"token\":\"$CALLBACK_TOKEN\",\"ok\":$1,\"ip\":\"${IP:-}\",\"domain\":\"${DOMAIN:-}\",\"setup_url\":\"${SETUP_URL:-}\",\"connector_url\":\"$cu\",\"log_tail\":\"$tail\"}"
  curl -fsS -m 20 -X POST "$CALLBACK_URL" -H "Content-Type: application/json" --data-binary @- <<<"$curl_body" >/dev/null || true
}
fail() { echo "FAILED: $1"; report false; exit 1; }

IP=$(curl -fsS -m 10 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address) || fail "no public IP"
DOMAIN="${CUSTOM_DOMAIN:-$(echo "$IP" | tr . -).sslip.io}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq unzip curl || fail "apt"
curl -fsS -m 300 "$KIT_URL" | base64 -d > /root/sweetspot-system.zip || fail "kit download"
echo "$KIT_SHA256  /root/sweetspot-system.zip" | sha256sum -c - || fail "kit checksum"
rm -rf /root/sweetspot-project && unzip -q /root/sweetspot-system.zip -d /root || fail "unzip"
bash /root/sweetspot-project/install.sh "$DOMAIN" || fail "Sweet Spot install"
SETUP_URL="https://$DOMAIN/setup#t=$(sed -n 's/^SETUP_TOKEN=//p' /etc/sweetspot-bot.env)"
# The same connector as the BCU server (app tools only), so Claude can manage this server too. Reached at /mcp.
(cd /root && curl -fsSL "$CONNECTOR_TAR" | tar xz && sh asterisk-connector-main/setup/install-on-asterisk-server.sh "$DOMAIN") || echo "connector install failed (Sweet Spot itself is fine)"
report true
"""


def cmd_create(_):
    b = json.loads(sys.stdin.read() or "{}")
    if b.get("confirm") is not True:
        raise Refused("Not created: confirm must be true (this creates a paid server, about $12 a month).")
    e = env()
    host = e.get("PUBLIC_HOST", "")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise Refused("PUBLIC_HOST is missing in /etc/claude-cloud.env; rerun the connector installer.")
    kit = kit_info()
    if not kit:
        raise Refused(f"No kit uploaded yet. Upload sweetspot-system.zip to {KIT} first.")
    if kit["bytes"] > 20 * 1024 * 1024:
        raise Refused("The kit is over 20 MB; that isn't the Sweet Spot kit.")
    domain = str(b.get("custom_domain") or "").strip().lower()
    if domain and not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        raise Refused("That custom domain doesn't look right.")
    existing = droplets()
    if existing:
        x = existing[0]
        raise Refused(f"A Sweet Spot server already exists: {x['name']} {x['ip'] or ''} ({x['status']}). Only one is allowed.")
    s = load_state()
    if s.get("status") in ("creating", "installing") and time.time() - s.get("started_ts", 0) < 1800:
        raise Refused("A Sweet Spot server is already being created.")
    keys = [k["id"] for k in cmd_ssh_keys(None)]
    kit_code, cb_code = secrets.token_hex(24), secrets.token_hex(24)
    script = (USER_DATA.replace("__KIT_URL__", f"https://{host}/cloud/kit/{kit_code}")
              .replace("__KIT_SHA256__", kit["sha256"]).replace("__CALLBACK_URL__", f"https://{host}/cloud/callback")
              .replace("__CALLBACK_TOKEN__", cb_code).replace("__CUSTOM_DOMAIN__", domain))
    new = {"status": "creating", "started_ts": time.time(), "started_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "kit_code_hash": sha(kit_code), "callback_code_hash": sha(cb_code), "codes_expire": time.time() + CODE_HOURS * 3600,
           "kit_downloads": 0, "kit_sha256": kit["sha256"], "domain": domain or None}
    save_state(new)
    try:
        r = do("POST", "/droplets", {**DROPLET, "ssh_keys": keys, "user_data": script})
    except Refused as ex:
        new.update(status="failed", error=str(ex))
        save_state(new)
        raise
    new.update(status="installing", droplet_id=r.get("droplet", {}).get("id"))
    save_state(new)
    return {"created": True, "droplet_id": new["droplet_id"], "name": DROPLET["name"], "region": "London (lon1)", "size": "2 GB",
            "ssh_keys_added": len(keys), "next": "It installs itself (about 8-10 minutes) and reports back; check with cloud_status."}


def _code_ok(s: dict, field: str, code: str) -> bool:
    return bool(code) and re.fullmatch(r"[0-9a-f]{48}", code) is not None and s.get(field) and \
        secrets.compare_digest(s[field], sha(code)) and time.time() < s.get("codes_expire", 0)


def cmd_kit(args):
    s = load_state()
    if not _code_ok(s, "kit_code_hash", args[0] if args else "") or s.get("kit_downloads", 0) >= KIT_DOWNLOADS:
        raise Refused("not found")
    s["kit_downloads"] = s.get("kit_downloads", 0) + 1
    save_state(s)
    with open(KIT, "rb") as f:
        sys.stdout.write(base64.b64encode(f.read()).decode())
    return None


def cmd_callback(_):
    b = json.loads(sys.stdin.read() or "{}")
    s = load_state()
    if s.get("status") != "installing" or not _code_ok(s, "callback_code_hash", str(b.get("token", ""))):
        raise Refused("not found")
    clean = lambda v, n: re.sub(r"[^\x20-\x7e]", "", str(v or ""))[:n]  # noqa: E731
    ip, domain = clean(b.get("ip"), 45), clean(b.get("domain"), 120).lower()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain or "x"):
        domain = ""
    setup = clean(b.get("setup_url"), 300)
    if setup and not re.fullmatch(rf"https://{re.escape(domain)}/setup#t=[0-9a-f]{{48}}", setup):
        setup = ""
    conn = clean(b.get("connector_url"), 300)
    if conn and re.fullmatch(rf"https://{re.escape(domain)}/mcp/[0-9a-f]{{64}}", conn):
        fd = os.open(CONNECTOR_URL, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(conn + "\n")
    ok = b.get("ok") is True
    s.update(status="ready" if ok else "failed", ip=ip or None, domain=domain or s.get("domain"), setup_url=setup or None,
             finished_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), log_tail=None if ok else clean(b.get("log_tail"), 1500),
             error=None if ok else "The new server's install failed; see log_tail.")
    s.pop("callback_code_hash", None)  # single use
    s.pop("kit_code_hash", None)
    save_state(s)
    return {"ok": True}


def cmd_set_token(_):
    if os.geteuid() != 0 or os.environ.get("SUDO_USER") == "claude-mcp":
        raise Refused("Run this yourself on the server: sudo claude-cloud-helper set-token")
    t = getpass.getpass("DigitalOcean token (input hidden): ").strip()
    if not re.fullmatch(r"dop_v1_[0-9A-Za-z]{40,128}", t):
        raise Refused("That doesn't look like a DigitalOcean token (it starts with dop_v1_).")
    e = env()
    e["DO_TOKEN"] = t
    write_env(e)
    print("Saved. Claude can now create the Sweet Spot server.")
    return None


def cmd_connector_url(_):
    if os.environ.get("SUDO_USER") == "claude-mcp":
        raise Refused("Only you can read this, on the server: sudo claude-cloud-helper connector-url")
    try:
        print(open(CONNECTOR_URL).read().strip())
    except OSError:
        raise Refused("No new server connector yet.")
    return None


# ------------------------------------------------------------------ private web page (/cloud/ui)
UI_LOCK_FAILS, UI_LOCK_SECONDS = 10, 900


def _ui_auth(key: str) -> None:
    """The page's key must match UI_KEY; 10 wrong keys in 15 minutes locks the page for everyone."""
    s = load_state()
    now = time.time()
    fails = [t for t in s.get("ui_fails", []) if now - t < UI_LOCK_SECONDS]
    if len(fails) >= UI_LOCK_FAILS:
        raise Refused("locked: too many wrong keys, try again in 15 minutes")
    expected = env().get("UI_KEY", "")
    if len(expected) >= 32 and isinstance(key, str) and secrets.compare_digest(sha(key), sha(expected)):
        return
    s["ui_fails"] = fails + [now]
    save_state(s)
    raise Refused("wrong key")


def cmd_ui(args):
    b = json.loads(sys.stdin.read() or "{}")
    _ui_auth(b.get("key", ""))
    what = args[0] if args else ""
    if what == "status":
        out = cmd_status(None)
        try:
            out["new_server_connector_url"] = open(CONNECTOR_URL).read().strip()  # the page owner may see it; Claude never calls this
        except OSError:
            out["new_server_connector_url"] = None
        out["install"].pop("new_server_connector", None)
        return out
    if what == "token":
        t = str(b.get("token", "")).strip()
        if not re.fullmatch(r"dop_v1_[0-9A-Za-z]{40,128}", t):
            raise Refused("That doesn't look like a DigitalOcean token (it starts with dop_v1_).")
        e = env()
        e["DO_TOKEN"] = t
        write_env(e)
        try:
            n = len(cmd_ssh_keys(None))
        except Refused as ex:
            return {"saved": True, "works": False, "message": str(ex)}
        return {"saved": True, "works": True, "message": f"Token works. {n} SSH key(s) on the account will go on the new server."}
    if what == "create":
        sys.stdin = io.StringIO(json.dumps({"confirm": b.get("confirm") is True, "custom_domain": b.get("custom_domain") or ""}))
        return cmd_create(None)
    raise Refused("unknown page action")


def cmd_ui_kit(_):
    first = sys.stdin.buffer.readline(4096)
    try:
        key = json.loads(first or b"{}").get("key", "")
    except ValueError:
        key = ""
    _ui_auth(key)
    data = sys.stdin.buffer.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024 or not data.startswith(b"PK\x03\x04"):
        raise Refused("That isn't a zip file (or it's over 25 MB).")
    import zipfile  # noqa: PLC0415
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
    except zipfile.BadZipFile:
        raise Refused("That zip file is damaged.")
    if "sweetspot-project/install.sh" not in names or any(n.startswith("/") or ".." in n.split("/") for n in names):
        raise Refused("That isn't the Sweet Spot kit (sweetspot-project/install.sh is missing).")
    tmp = KIT + ".upload"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, KIT)
    return {"saved": True, **kit_info()}


def cmd_ui_link(_):
    if os.environ.get("SUDO_USER") == "claude-mcp":
        raise Refused("Only you can read this, on the server: sudo claude-cloud-helper ui-link")
    e = env()
    if not e.get("UI_KEY") or not e.get("PUBLIC_HOST"):
        raise Refused("No page key yet: rerun the connector installer.")
    print(f"https://{e['PUBLIC_HOST']}/cloud/ui#k={e['UI_KEY']}")
    return None


COMMANDS = {"status": cmd_status, "ssh-keys": cmd_ssh_keys, "create": cmd_create, "kit": cmd_kit, "callback": cmd_callback,
            "set-token": cmd_set_token, "connector-url": cmd_connector_url,
            "ui": cmd_ui, "ui-kit": cmd_ui_kit, "ui-link": cmd_ui_link}


def main(argv):
    if os.geteuid() != 0:
        print("claude-cloud-helper must run as root", file=sys.stderr)
        return 2
    if not argv or argv[0] not in COMMANDS:
        print("usage: claude-cloud-helper " + "|".join(COMMANDS), file=sys.stderr)
        return 2
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        out = COMMANDS[argv[0]](argv[1:])
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 1
    if out is not None:
        print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
