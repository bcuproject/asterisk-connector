#!/usr/bin/env python3
"""claude-cloud-helper: lets the connector create shop servers on DigitalOcean (one per shop), and nothing else.

Root-owned. The connector's service user may run it through sudo; the DigitalOcean token lives in
/etc/claude-cloud.env (root-only), so the connector itself never sees it.

Each shop (Sweet Spot, or any other) gets its own server: London, 2 GB, Ubuntu 24.04, named shop-<slug>,
tagged bcu-shop + shop-<slug>. It installs the shop ordering system (and its own connector) by itself,
then reports back. At most one server per shop, and at most MAX_SHOPS (default 10) in total.

  status                     token set?, kit uploaded?, every shop's server and install progress
  ssh-keys                   the account's SSH key names (they all go on new servers)
  create      (stdin JSON)   {"confirm": true, "shop_name": "Burger Barn", "slug": "burgerbarn", "custom_domain": "", "phone_ordering": false}
  kit <code> [menu]          base64 of the kit (or the shop's menu), for the new server only (one-time code, 3 hours)
  callback    (stdin JSON)   a new server's report when installing finishes (one-time code)
  set-token                  (admin, interactive) store the DigitalOcean token with a hidden prompt
  connector-url <slug>       (admin) print a shop server's connector address, to add in Claude settings
  ui <status|token|create|menu>   (stdin JSON with "key") the private web page's actions; key = UI_KEY in /etc/claude-cloud.env
  ui-kit                     (stdin: one JSON line with "key", then the zip bytes) the page's kit upload
  ui-link                    (admin) print the private page's address
  ui update                  the page's Update button: reinstall the connector from GitHub (UPDATE_BRANCH), in the background

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
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENV = "/etc/claude-cloud.env"
STATE_DIR = "/var/lib/claude-cloud"
KIT = os.path.join(STATE_DIR, "sweetspot-system.zip")
STATE = os.path.join(STATE_DIR, "state.json")
MENUS = os.path.join(STATE_DIR, "menus")
CONNECTORS = os.path.join(STATE_DIR, "connectors")
ALL_TAG = "bcu-shop"
SIZE = {"region": "lon1", "size": "s-1vcpu-2gb", "image": "ubuntu-24-04-x64", "monitoring": True, "ipv6": False}
CODE_HOURS = 3
KIT_DOWNLOADS = 10   # kit + menu, with a few retries
API = "https://api.digitalocean.com/v2"
SLUG_RE = r"[a-z0-9][a-z0-9-]{1,22}[a-z0-9]"
NAME_RE = r"[A-Za-z0-9][A-Za-z0-9 &.-]{0,38}[A-Za-z0-9.]"
DOMAIN_RE = r"[a-z0-9-]+(\.[a-z0-9-]+)+"


class Refused(Exception):
    pass


# ------------------------------------------------------------------ files
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


def _write_private(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_env(values: dict) -> None:
    head = "# DigitalOcean access for claude-cloud-helper. Root-only. Never paste this token into chat.\n"
    _write_private(ENV, (head + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n").encode())


def load_state() -> dict:
    try:
        with open(STATE) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("shops", {})
    return s


def save_state(s: dict) -> None:
    _write_private(STATE, json.dumps(s).encode())


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


# ------------------------------------------------------------------ DigitalOcean (allow-list)
def do(method: str, path: str, body: dict | None = None) -> dict:
    base = path.split("?", 1)[0]
    allowed = (method == "GET" and (base == "/account/keys" or re.fullmatch(r"/droplets(/\d+)?", base) is not None)) or \
              (method == "POST" and base == "/droplets")
    if not allowed:
        raise Refused("That DigitalOcean action isn't allowed.")
    token = env().get("DO_TOKEN", "")
    if not token:
        raise Refused("No DigitalOcean token yet: save it on the private page (or: sudo claude-cloud-helper set-token).")
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


def droplets(tag: str = ALL_TAG) -> list:
    d = do("GET", f"/droplets?tag_name={tag}&per_page=50").get("droplets", [])
    out = []
    for x in d:
        ip = next((n["ip_address"] for n in x.get("networks", {}).get("v4", []) if n.get("type") == "public"), None)
        slug = next((t[5:] for t in x.get("tags", []) if t.startswith("shop-")), None)
        out.append({"id": x["id"], "name": x["name"], "shop": slug, "status": x["status"], "ip": ip,
                    "region": x.get("region", {}).get("slug"), "size": x.get("size_slug"), "created_at": x.get("created_at")})
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


def menu_path(slug: str) -> str:
    return os.path.join(MENUS, f"{slug}.json")


def connector_path(slug: str) -> str:
    return os.path.join(CONNECTORS, f"{slug}.txt")


# ------------------------------------------------------------------ status
PUBLIC_SHOP_FIELDS = ("shop_name", "status", "droplet_id", "started_at", "finished_at", "domain", "ip", "setup_url", "error", "log_tail", "phone_ordering")


def cmd_status(_):
    e, s = env(), load_state()
    info = {"token_set": bool(e.get("DO_TOKEN")), "public_host": e.get("PUBLIC_HOST") or None, "kit": kit_info(),
            "max_shops": int(e.get("MAX_SHOPS") or 10), "servers": None, "shops": {}}
    for slug, sh in s["shops"].items():
        info["shops"][slug] = {k: sh.get(k) for k in PUBLIC_SHOP_FIELDS if sh.get(k) is not None}
        info["shops"][slug]["connector"] = ("ready (address: sudo claude-cloud-helper connector-url " + slug + ")") if os.path.exists(connector_path(slug)) else None
    info["menus_uploaded"] = sorted(f[:-5] for f in os.listdir(MENUS) if f.endswith(".json")) if os.path.isdir(MENUS) else []
    if info["token_set"]:
        try:
            info["servers"] = droplets()
        except Refused as ex:
            info["servers_error"] = str(ex)
    if not info["kit"]:
        info["next_step"] = "Upload the kit (sweetspot-system.zip) on the private page."
    elif not info["token_set"]:
        info["next_step"] = "Save the DigitalOcean token on the private page."
    return info


def cmd_ssh_keys(_):
    return [{"id": k["id"], "name": k["name"]} for k in do("GET", "/account/keys?per_page=50").get("ssh_keys", [])]


# ------------------------------------------------------------------ create
USER_DATA = r"""#!/bin/bash
# Shop server: installs itself once, then reports back. Written by claude-cloud-helper.
set -uo pipefail
exec > /var/log/shop-provision.log 2>&1
KIT_URL='__KIT_URL__'
KIT_SHA256='__KIT_SHA256__'
MENU_SHA256='__MENU_SHA256__'
CALLBACK_URL='__CALLBACK_URL__'
CALLBACK_TOKEN='__CALLBACK_TOKEN__'
CUSTOM_DOMAIN='__CUSTOM_DOMAIN__'
SHOP_NAME='__SHOP_NAME__'
VOICE='__VOICE__'
CONNECTOR_TAR='https://github.com/bcuproject/asterisk-connector/archive/refs/heads/main.tar.gz'

report() {  # $1 = true/false
  local tail body cu=""
  # One line, no quotes/backslashes/control characters: it goes inside a JSON string (line breaks broke it: 400).
  tail=$(tail -n 30 /var/log/shop-provision.log | grep -v -e '/mcp/' -e '#t=' | tr '\n\t' '  ' | tr -d '\000-\037"\\' | tail -c 1500)
  [ -f /etc/asterisk-connector.env ] && cu="https://$DOMAIN/mcp/$(sed -n 's/^CONNECTOR_TOKEN=//p' /etc/asterisk-connector.env)"
  body="{\"token\":\"$CALLBACK_TOKEN\",\"ok\":$1,\"ip\":\"${IP:-}\",\"domain\":\"${DOMAIN:-}\",\"setup_url\":\"${SETUP_URL:-}\",\"connector_url\":\"$cu\",\"log_tail\":\"$tail\"}"
  curl -fsS -m 20 -X POST "$CALLBACK_URL" -H "Content-Type: application/json" --data-binary @- <<<"$body" >/dev/null || true
}
fail() { echo "FAILED: $1"; report false; exit 1; }

IP=$(curl -fsS -m 10 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address) || fail "no public IP"
DOMAIN="${CUSTOM_DOMAIN:-$(echo "$IP" | tr . -).sslip.io}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq unzip curl || fail "apt"
curl -fsS -m 300 "$KIT_URL" | base64 -d > /root/shop-kit.zip || fail "kit download"
echo "$KIT_SHA256  /root/shop-kit.zip" | sha256sum -c - || fail "kit checksum"
rm -rf /root/sweetspot-project && unzip -q /root/shop-kit.zip -d /root || fail "unzip"
MENU_FILE=""
if [ -n "$MENU_SHA256" ]; then
  curl -fsS -m 120 "$KIT_URL/menu" | base64 -d > /root/menu.json || fail "menu download"
  echo "$MENU_SHA256  /root/menu.json" | sha256sum -c - || fail "menu checksum"
  MENU_FILE=/root/menu.json
fi
SHOP_NAME="$SHOP_NAME" MENU_FILE="$MENU_FILE" VOICE="$VOICE" bash /root/sweetspot-project/install.sh "$DOMAIN" || fail "shop install"
SETUP_URL="https://$DOMAIN/setup#t=$(sed -n 's/^SETUP_TOKEN=//p' /etc/sweetspot-bot.env)"
# The same connector as the phone server (app tools only), so Claude can manage this server too. Reached at /mcp.
(cd /root && curl -fsSL "$CONNECTOR_TAR" | tar xz && sh asterisk-connector-main/setup/install-on-asterisk-server.sh "$DOMAIN") || echo "connector install failed (the shop itself is fine)"
report true
"""


def cmd_create(_):
    b = json.loads(sys.stdin.read() or "{}")
    if b.get("confirm") is not True:
        raise Refused("Not created: confirm must be true (each shop server costs about $12 a month).")
    e = env()
    host = e.get("PUBLIC_HOST", "")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(:[0-9]{2,5})?", host):  # the connector may run on its own port, e.g. :8788
        raise Refused("PUBLIC_HOST is missing in /etc/claude-cloud.env; rerun the connector installer.")
    name = re.sub(r"\s+", " ", str(b.get("shop_name") or "").strip())
    if not re.fullmatch(NAME_RE, name):
        raise Refused("Shop name: 2-40 letters, numbers, spaces, & . or -.")
    slug = str(b.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))[:24].strip("-")
    if not re.fullmatch(SLUG_RE, slug):
        raise Refused("Short name: 3-24 lowercase letters, numbers or -.")
    domain = str(b.get("custom_domain") or "").strip().lower()
    if domain and not re.fullmatch(DOMAIN_RE, domain):
        raise Refused("That web address doesn't look right.")
    voice = b.get("phone_ordering") is True
    kit = kit_info()
    if not kit:
        raise Refused("No kit uploaded yet: upload sweetspot-system.zip on the private page first.")
    if kit["bytes"] > 25 * 1024 * 1024:
        raise Refused("The kit is over 25 MB; that isn't the shop kit.")
    menu_sha = ""
    if os.path.exists(menu_path(slug)):
        with open(menu_path(slug), "rb") as f:
            menu_sha = hashlib.sha256(f.read()).hexdigest()
    s = load_state()
    mine = droplets(f"shop-{slug}")
    if mine:
        x = mine[0]
        raise Refused(f"{name} already has a server: {x['name']} {x['ip'] or ''} ({x['status']}). Only one per shop.")
    sh = s["shops"].get(slug, {})
    if sh.get("status") in ("creating", "installing") and time.time() - sh.get("started_ts", 0) < 1800:
        raise Refused(f"{name}'s server is already being created.")
    limit = int(e.get("MAX_SHOPS") or 10)
    if len(droplets()) >= limit:
        raise Refused(f"There are already {limit} shop servers (the limit). An admin can raise MAX_SHOPS in /etc/claude-cloud.env.")
    keys = [k["id"] for k in cmd_ssh_keys(None)]
    kit_code, cb_code = secrets.token_hex(24), secrets.token_hex(24)
    script = USER_DATA
    for k, v in {"__KIT_URL__": f"https://{host}/cloud/kit/{kit_code}", "__KIT_SHA256__": kit["sha256"], "__MENU_SHA256__": menu_sha,
                 "__CALLBACK_URL__": f"https://{host}/cloud/callback", "__CALLBACK_TOKEN__": cb_code, "__CUSTOM_DOMAIN__": domain,
                 "__SHOP_NAME__": name, "__VOICE__": "1" if voice else "0"}.items():
        script = script.replace(k, v)
    s["shops"][slug] = {"shop_name": name, "status": "creating", "started_ts": time.time(), "started_at": now_utc(), "phone_ordering": voice,
                        "kit_code_hash": sha(kit_code), "callback_code_hash": sha(cb_code), "codes_expire": time.time() + CODE_HOURS * 3600,
                        "kit_downloads": 0, "domain": domain or None}
    save_state(s)
    try:
        r = do("POST", "/droplets", {**SIZE, "name": f"shop-{slug}", "tags": [ALL_TAG, f"shop-{slug}"], "ssh_keys": keys, "user_data": script})
    except Refused as ex:
        s = load_state()
        s["shops"][slug].update(status="failed", error=str(ex))
        save_state(s)
        raise
    s = load_state()
    s["shops"][slug].update(status="installing", droplet_id=r.get("droplet", {}).get("id"))
    save_state(s)
    sweet = name.lower() == "sweet spot"
    return {"created": True, "shop": name, "slug": slug, "droplet_id": s["shops"][slug]["droplet_id"], "name": f"shop-{slug}",
            "region": "London (lon1)", "size": "2 GB", "ssh_keys_added": len(keys),
            "menu": "uploaded menu" if menu_sha else ("Sweet Spot's menu" if sweet else "empty (fill it in the back panel)"),
            "phone_ordering": voice, "next": "It installs itself (about 8-10 minutes) and reports back; check with cloud_status."}


# ------------------------------------------------------------------ the new server's downloads and report
def _find(field: str, code: str):
    if not (isinstance(code, str) and re.fullmatch(r"[0-9a-f]{48}", code)):
        return None, None
    s = load_state()
    for slug, sh in s["shops"].items():
        if sh.get(field) and secrets.compare_digest(sh[field], sha(code)) and time.time() < sh.get("codes_expire", 0):
            return s, slug
    return None, None


def cmd_kit(args):
    s, slug = _find("kit_code_hash", args[0] if args else "")
    if not slug or s["shops"][slug].get("kit_downloads", 0) >= KIT_DOWNLOADS:
        raise Refused("not found")
    s["shops"][slug]["kit_downloads"] = s["shops"][slug].get("kit_downloads", 0) + 1
    save_state(s)
    path = menu_path(slug) if len(args) > 1 and args[1] == "menu" else KIT
    if not os.path.exists(path):
        raise Refused("not found")
    with open(path, "rb") as f:
        sys.stdout.write(base64.b64encode(f.read()).decode())
    return None


def cmd_callback(_):
    b = json.loads(sys.stdin.read() or "{}")
    s, slug = _find("callback_code_hash", str(b.get("token", "")))
    if not slug or s["shops"][slug].get("status") != "installing":
        raise Refused("not found")
    sh = s["shops"][slug]
    clean = lambda v, n: re.sub(r"[^\x20-\x7e]", "", str(v or ""))[:n]  # noqa: E731
    ip, domain = clean(b.get("ip"), 45), clean(b.get("domain"), 120).lower()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain or "x"):
        domain = ""
    setup = clean(b.get("setup_url"), 300)
    if setup and not re.fullmatch(rf"https://{re.escape(domain)}/setup#t=[0-9a-f]{{48}}", setup):
        setup = ""
    conn = clean(b.get("connector_url"), 300)
    if conn and re.fullmatch(rf"https://{re.escape(domain)}/mcp/[0-9a-f]{{64}}", conn):
        _write_private(connector_path(slug), (conn + "\n").encode())
    ok = b.get("ok") is True
    sh.update(status="ready" if ok else "failed", ip=ip or None, domain=domain or sh.get("domain"), setup_url=setup or None,
              finished_at=now_utc(), log_tail=None if ok else clean(b.get("log_tail"), 1500),
              error=None if ok else "The new server's install failed; see log_tail.")
    sh.pop("callback_code_hash", None)  # single use
    sh.pop("kit_code_hash", None)
    save_state(s)
    return {"ok": True}


# ------------------------------------------------------------------ admin-only (never through the connector)
def _not_connector(what: str):
    if os.environ.get("SUDO_USER") == "claude-mcp":
        raise Refused(f"Only you can do this, on the server: sudo claude-cloud-helper {what}")


def cmd_set_token(_):
    _not_connector("set-token")
    t = getpass.getpass("DigitalOcean token (input hidden): ").strip()
    if not re.fullmatch(r"dop_v1_[0-9A-Za-z]{40,128}", t):
        raise Refused("That doesn't look like a DigitalOcean token (it starts with dop_v1_).")
    e = env()
    e["DO_TOKEN"] = t
    write_env(e)
    print("Saved. Shop servers can now be created.")
    return None


def cmd_connector_url(args):
    _not_connector("connector-url <shop>")
    slug = args[0] if args else ""
    try:
        print(open(connector_path(slug)).read().strip())
    except OSError:
        raise Refused(f"No connector yet for {slug or '(give the shop short name)'}.")
    return None


def cmd_ui_link(_):
    _not_connector("ui-link")
    e = env()
    if not e.get("UI_KEY") or not e.get("PUBLIC_HOST"):
        raise Refused("No page key yet: rerun the connector installer.")
    print(f"https://{e['PUBLIC_HOST']}/cloud/ui#k={e['UI_KEY']}")
    return None


# ------------------------------------------------------------------ Update button (connector + this page)
UPDATE_LOG = "/var/log/claude-connector-update.log"
UPDATE_REPO = "https://github.com/bcuproject/asterisk-connector/archive/refs/heads"
SECRET_LINE = re.compile(r"/mcp/|#k=|token|key|secret|password", re.I)


def update_status() -> dict:
    u = load_state().get("update") or {}
    if not u:
        return {"running": False, "last": None}
    running = subprocess.run(["systemctl", "is-active", "--quiet", u["unit"]]).returncode == 0
    result, lines = None, []
    try:
        with open(UPDATE_LOG, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    exits = [ln for ln in lines if ln.startswith("EXIT=")]
    if exits:
        result = "ok" if exits[-1] == "EXIT=0" else "failed"
    # Never show lines that could hold a password (the installer prints the connector address and page link).
    safe = [ln for ln in lines if ln.strip() and not ln.startswith("EXIT=") and not SECRET_LINE.search(ln)][-8:]
    return {"running": running and result is None, "started_at": u.get("started_at"), "branch": u.get("branch"),
            "result": result, "detail": safe if result == "failed" else safe[-2:]}


def start_update() -> dict:
    st = update_status()
    if st["running"]:
        raise Refused("An update is already running.")
    branch = env().get("UPDATE_BRANCH") or "sweetspot-server"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", branch):
        raise Refused("UPDATE_BRANCH in /etc/claude-cloud.env isn't a plain branch name.")
    unit = f"claude-connector-update-{int(time.time())}"
    fd = os.open(UPDATE_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # root-only: the installer prints passwords
    os.close(fd)
    script = (f'set -e; d="$(mktemp -d)"; cd "$d"; curl -fsSL --max-time 300 {UPDATE_REPO}/{branch}.tar.gz | tar xz; '
              f'sh ./*/setup/install-on-asterisk-server.sh')
    # Its own systemd unit: the installer restarts the connector (and this helper's parent) without cutting itself off.
    r = subprocess.run(["systemd-run", f"--unit={unit}", "--collect", "--quiet", "/bin/sh", "-c",
                        f"( {script} ) >> {UPDATE_LOG} 2>&1; echo EXIT=$? >> {UPDATE_LOG}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise Refused("Couldn't start the update.")
    s = load_state()
    s["update"] = {"unit": unit, "started_at": now_utc(), "branch": branch}
    save_state(s)
    return {"started": True, "branch": branch, "message": "Updating — about 2 minutes. The page reconnects by itself."}


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
        out["update"] = update_status()
        for slug, sh in out["shops"].items():  # the page owner may see these addresses; Claude's tools never do
            try:
                sh["connector_url"] = open(connector_path(slug)).read().strip()
            except OSError:
                sh["connector_url"] = None
            sh.pop("connector", None)
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
        return {"saved": True, "works": True, "message": f"Token works. {n} SSH key(s) on the account will go on new servers."}
    if what == "menu":
        slug = str(b.get("slug") or "")
        if not re.fullmatch(SLUG_RE, slug):
            raise Refused("Short name: 3-24 lowercase letters, numbers or -.")
        menu = b.get("menu")
        if not isinstance(menu, dict) or menu.get("schema") not in ("shop-menu/1", "sweetspot-menu/1") or not isinstance(menu.get("categories"), list):
            raise Refused("That isn't a menu file (it needs \"schema\": \"shop-menu/1\" and categories; see shop-menu-template.json).")
        items = sum(len(c.get("items") or []) for c in menu["categories"] if isinstance(c, dict))
        if not items:
            raise Refused("That menu has no items.")
        _write_private(menu_path(slug), json.dumps(menu).encode())
        return {"saved": True, "slug": slug, "sections": len(menu["categories"]), "items": items}
    if what == "update":
        return start_update()
    if what == "create":
        sys.stdin = io.StringIO(json.dumps({"confirm": b.get("confirm") is True, "shop_name": b.get("shop_name"), "slug": b.get("slug"),
                                            "custom_domain": b.get("custom_domain") or "", "phone_ordering": b.get("phone_ordering") is True}))
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
        raise Refused("That isn't the shop kit (sweetspot-project/install.sh is missing).")
    _write_private(KIT, data)
    return {"saved": True, **kit_info()}


COMMANDS = {"status": cmd_status, "ssh-keys": cmd_ssh_keys, "create": cmd_create, "kit": cmd_kit, "callback": cmd_callback,
            "set-token": cmd_set_token, "connector-url": cmd_connector_url, "ui": cmd_ui, "ui-kit": cmd_ui_kit, "ui-link": cmd_ui_link}


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
