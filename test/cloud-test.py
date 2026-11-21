"""Tests claude-cloud-helper against a stand-in DigitalOcean API (no real account, no real server).
   python3 test/cloud-test.py        (run as root, like the real helper)"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cloud", os.path.join(HERE, "..", "setup", "claude-cloud-helper.py"))
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)

tmp = tempfile.mkdtemp()
cloud.ENV, cloud.STATE_DIR = os.path.join(tmp, "cloud.env"), os.path.join(tmp, "state")
cloud.KIT, cloud.STATE = os.path.join(cloud.STATE_DIR, "kit.zip"), os.path.join(cloud.STATE_DIR, "state.json")
cloud.MENUS, cloud.CONNECTORS = os.path.join(cloud.STATE_DIR, "menus"), os.path.join(cloud.STATE_DIR, "connectors")
TOKEN = "dop_v1_" + "ab12" * 16
calls, droplets = [], []


class FakeDO(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        calls.append(("GET", self.path, self.headers.get("Authorization")))
        u = urlparse(self.path)
        if u.path == "/v2/account/keys":
            return self.reply(200, {"ssh_keys": [{"id": 11, "name": "Max laptop", "fingerprint": "x"}]})
        if u.path == "/v2/droplets":
            tag = parse_qs(u.query).get("tag_name", [""])[0]
            return self.reply(200, {"droplets": [d for d in droplets if tag in d["tags"]]})
        self.reply(404, {"message": "nope"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        calls.append(("POST", self.path, self.headers.get("Authorization"), body))
        did = 900 + len(droplets)
        droplets.append({"id": did, "name": body["name"], "tags": body["tags"], "status": "new", "networks": {"v4": []}, "region": {"slug": body["region"]}, "size_slug": body["size"]})
        self.reply(202, {"droplet": {"id": did}})


srv = HTTPServer(("127.0.0.1", 0), FakeDO)
threading.Thread(target=srv.serve_forever, daemon=True).start()
cloud.API = f"http://127.0.0.1:{srv.server_port}/v2"

passed = 0
def check(cond, what):
    global passed
    if not cond:
        print("  ✘", what); sys.exit(1)
    print("  ✔", what); passed += 1


def run(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin), out, err
    try:
        code = cloud.main(argv)
    finally:
        sys.stdin, sys.stdout, sys.stderr = old
    return code, out.getvalue(), err.getvalue()


SWEET = {"confirm": True, "shop_name": "Sweet Spot", "slug": "sweetspot", "phone_ordering": True}
print("Before anything is set up")
code, out, _ = run(["status"])
check(code == 0 and not json.loads(out)["token_set"] and json.loads(out)["kit"] is None, "status says what's missing")
code, _, err = run(["create"], json.dumps(SWEET))
check(code == 1 and "PUBLIC_HOST" in err, "can't create before the installer has set the web address")
cloud.write_env({"DO_TOKEN": "", "PUBLIC_HOST": "206-189-123-28.sslip.io", "UI_KEY": "u" * 48})
code, _, err = run(["create"], json.dumps(SWEET))
check(code == 1 and "kit" in err.lower(), "can't create without the kit")
os.makedirs(cloud.STATE_DIR, exist_ok=True)
kit_bytes = os.urandom(300_000)
open(cloud.KIT, "wb").write(kit_bytes)
code, _, err = run(["create"], json.dumps(SWEET))
check(code == 1 and "token" in err.lower(), "can't create without the DigitalOcean token")
cloud.write_env({"DO_TOKEN": TOKEN, "PUBLIC_HOST": "206-189-123-28.sslip.io", "UI_KEY": "u" * 48})
check(oct(os.stat(cloud.ENV).st_mode & 0o777) == "0o600", "token file is root-only (600)")

print("Safety")
code, _, err = run(["create"], json.dumps({**SWEET, "confirm": False}))
check(code == 1 and "confirm" in err, "creating needs confirm: true")
for m, p in (("DELETE", "/droplets/900"), ("POST", "/droplets/900/actions"), ("PUT", "/droplets/900"), ("GET", "/account")):
    try:
        cloud.do(m, p, {}); check(False, f"{m} {p} refused")
    except cloud.Refused:
        pass
check(True, "delete, power, resize, rebuild and other DigitalOcean calls can't be sent at all")
code, _, err = run(["create"], json.dumps({**SWEET, "custom_domain": "evil.com; rm -rf /"}))
check(code == 1 and "address" in err, "a bad web address is refused")
code, _, err = run(["create"], json.dumps({**SWEET, "shop_name": "Bad'; rm -rf / #"}))
check(code == 1 and "Shop name" in err, "a shop name that could break the setup script is refused")

print("Sweet Spot server")
code, out, err = run(["create"], json.dumps(SWEET))
r = json.loads(out or "{}")
check(code == 0 and r.get("droplet_id") == 900 and r["menu"] == "Sweet Spot's menu" and r["phone_ordering"], f"Sweet Spot server created with its own menu and phone ordering {err}")
body = [c for c in calls if c[0] == "POST"][0][3]
check(body["name"] == "shop-sweetspot" and body["tags"] == ["bcu-shop", "shop-sweetspot"] and body["region"] == "lon1"
      and body["size"] == "s-1vcpu-2gb" and body["image"] == "ubuntu-24-04-x64" and body["ssh_keys"] == [11], "London, 2 GB, Ubuntu 24.04, tagged, SSH key added")
ud = body["user_data"]
check("SHOP_NAME='Sweet Spot'" in ud and "VOICE='1'" in ud and "MENU_SHA256=''" in ud, "setup script installs Sweet Spot with phone ordering")
check(TOKEN not in ud and TOKEN not in out and TOKEN not in err, "the DigitalOcean token is never in the script or the output")
check(subprocess.run(["bash", "-n"], input=ud, text=True).returncode == 0, "setup script is valid bash")
sweet_kit = re.search(r"/cloud/kit/([0-9a-f]{48})", ud).group(1)
sweet_cb = re.search(r"CALLBACK_TOKEN='([0-9a-f]{48})'", ud).group(1)
check(sweet_kit not in open(cloud.STATE).read() and sweet_cb not in open(cloud.STATE).read(), "only hashes of the one-time codes are stored")
code, _, err = run(["create"], json.dumps(SWEET))
check(code == 1 and "already has a server" in err, "a second Sweet Spot server is refused")

print("A second shop, with its own menu")
menu = {"schema": "shop-menu/1", "categories": [{"id": "burgers", "items": [{"id": "b1", "name": "Classic Burger", "price_pence": 799}]}]}
code, out, _ = run(["ui", "menu"], json.dumps({"key": "u" * 48, "slug": "burger-barn", "menu": menu}))
check(code == 0 and json.loads(out)["items"] == 1, "menu uploaded for Burger Barn")
code, _, err = run(["ui", "menu"], json.dumps({"key": "u" * 48, "slug": "burger-barn", "menu": {"hello": 1}}))
check(code == 1 and "menu" in err, "something that isn't a menu is refused")
code, out, err = run(["create"], json.dumps({"confirm": True, "shop_name": "Burger Barn"}))
r = json.loads(out or "{}")
check(code == 0 and r["slug"] == "burger-barn" and r["menu"] == "uploaded menu" and not r["phone_ordering"], f"Burger Barn server created (short name made from the name) {err}")
ud2 = [c for c in calls if c[0] == "POST"][1][3]["user_data"]
check("SHOP_NAME='Burger Barn'" in ud2 and "VOICE='0'" in ud2 and hashlib.sha256(json.dumps(menu).encode()).hexdigest() in ud2, "its setup script carries its name and menu checksum, phone ordering off")
bb_kit = re.search(r"/cloud/kit/([0-9a-f]{48})", ud2).group(1)

print("Downloads (by each new server)")
code, out, _ = run(["kit", sweet_kit])
check(code == 0 and base64.b64decode(out) == kit_bytes, "kit downloads intact with the right code")
code, out, _ = run(["kit", bb_kit, "menu"])
check(code == 0 and json.loads(base64.b64decode(out)) == menu, "Burger Barn's server gets Burger Barn's menu")
code, _, _ = run(["kit", sweet_kit, "menu"])
check(code == 1, "Sweet Spot's code can't fetch a menu it doesn't have")
code, _, _ = run(["kit", "0" * 48])
check(code == 1, "wrong code refused")
for _ in range(10):
    run(["kit", sweet_kit])
code, _, _ = run(["kit", sweet_kit])
check(code == 1, "download code stops after its limit")

print("Report back")
dom = "1-2-3-4.sslip.io"
good = {"token": sweet_cb, "ok": True, "ip": "1.2.3.4", "domain": dom, "setup_url": f"https://{dom}/setup#t={'c' * 48}", "connector_url": f"https://{dom}/mcp/{'d' * 64}"}
code, _, _ = run(["callback"], json.dumps({**good, "token": "0" * 48}))
check(code == 1, "report with a wrong code refused")
code, _, _ = run(["callback"], json.dumps(good))
check(code == 0, "Sweet Spot's report accepted")
code, out, _ = run(["status"])
st = json.loads(out)
check(st["shops"]["sweetspot"]["status"] == "ready" and st["shops"]["sweetspot"]["setup_url"] == good["setup_url"], "Sweet Spot shows ready with its setup link")
check(st["shops"]["burger-barn"]["status"] == "installing", "Burger Barn still installing")
check("d" * 64 not in out and st["shops"]["sweetspot"]["connector"].startswith("ready"), "the new connector's address is never shown to Claude's tools")
code, out, _ = run(["ui", "status"], json.dumps({"key": "u" * 48}))
check(json.loads(out)["shops"]["sweetspot"]["connector_url"] == good["connector_url"], "…but is shown on the private page")
code, _, _ = run(["callback"], json.dumps(good))
check(code == 1, "the report code works only once")

print("Shop limit")
cloud.write_env({"DO_TOKEN": TOKEN, "PUBLIC_HOST": "206-189-123-28.sslip.io", "UI_KEY": "u" * 48, "MAX_SHOPS": "2"})
code, _, err = run(["create"], json.dumps({"confirm": True, "shop_name": "Third Shop"}))
check(code == 1 and "limit" in err, "no more servers than MAX_SHOPS")

print("Update button")
runs = []
class Done:  # stand-in for subprocess results
    def __init__(self, rc): self.returncode, self.stdout, self.stderr = rc, "", ""
def fake_run(cmd, **kw):
    runs.append(cmd)
    if cmd[0] == "systemctl":
        return Done(0 if fake_run.active else 3)
    return Done(0)
fake_run.active = False
cloud.subprocess.run = fake_run
cloud.UPDATE_LOG = os.path.join(tmp, "update.log")
code, out, err = run(["ui", "update"], json.dumps({"key": "x" * 48}))
check(code == 1, "the Update button needs the page key")
code, out, err = run(["ui", "update"], json.dumps({"key": "u" * 48}))
cmd = runs[-1]
check(code == 0 and cmd[0] == "systemd-run" and "github.com/bcuproject/asterisk-connector/archive/refs/heads/sweetspot-server.tar.gz" in cmd[-1],
      "update runs in its own background unit, from your GitHub repo only")
check(oct(os.stat(cloud.UPDATE_LOG).st_mode & 0o777) == "0o600", "the update log is root-only (the installer prints passwords)")
fake_run.active = True
code, _, err = run(["ui", "update"], json.dumps({"key": "u" * 48}))
check(code == 1 and "already running" in err, "only one update at a time")
open(cloud.UPDATE_LOG, "w").write("== Install app\n URL:  https://h/mcp/" + "9" * 64 + "\n https://h/cloud/ui#k=" + "u" * 48 + "\nConnector is running on 127.0.0.1:8787\nEXIT=0\n")
fake_run.active = False
code, out, _ = run(["ui", "status"], json.dumps({"key": "u" * 48}))
u = json.loads(out)["update"]
check(u["result"] == "ok" and not u["running"] and "9" * 64 not in out and "#k=" not in out, "page shows the update worked, never the passwords it printed")
open(cloud.UPDATE_LOG, "w").write("npm ERR! something broke\nCONNECTOR_TOKEN=" + "e" * 64 + "\nEXIT=1\n")
code, out, _ = run(["ui", "status"], json.dumps({"key": "u" * 48}))
u = json.loads(out)["update"]
check(u["result"] == "failed" and "npm ERR! something broke" in u["detail"] and "e" * 64 not in out, "a failed update shows its error lines, minus anything secret")
cloud.write_env({"DO_TOKEN": TOKEN, "PUBLIC_HOST": "206-189-123-28.sslip.io", "UI_KEY": "u" * 48, "UPDATE_BRANCH": "main; rm -rf /"})
code, _, err = run(["ui", "update"], json.dumps({"key": "u" * 48}))
check(code == 1 and "branch" in err, "a strange branch name is refused")

print("Admin-only commands")
os.environ["SUDO_USER"] = "claude-mcp"
for c in (["connector-url", "sweetspot"], ["set-token"], ["ui-link"]):
    code, _, err = run(c)
    check(code == 1 and "Only you" in err, f"the connector can't run {c[0]}")
del os.environ["SUDO_USER"]

print(f"\nAll {passed} cloud checks passed.")
srv.shutdown()
