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

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cloud", os.path.join(HERE, "..", "setup", "claude-cloud-helper.py"))
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)

tmp = tempfile.mkdtemp()
cloud.ENV, cloud.STATE_DIR = os.path.join(tmp, "cloud.env"), os.path.join(tmp, "state")
cloud.KIT, cloud.STATE = os.path.join(cloud.STATE_DIR, "sweetspot-system.zip"), os.path.join(cloud.STATE_DIR, "state.json")
cloud.CONNECTOR_URL = os.path.join(cloud.STATE_DIR, "new-server-connector.txt")
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
        if self.path.startswith("/v2/account/keys"):
            return self.reply(200, {"ssh_keys": [{"id": 11, "name": "Max laptop", "fingerprint": "x", "public_key": "ssh-ed25519 AAA"}]})
        if self.path.startswith("/v2/droplets"):
            return self.reply(200, {"droplets": droplets})
        self.reply(404, {"message": "nope"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        calls.append(("POST", self.path, self.headers.get("Authorization"), body))
        droplets.append({"id": 999, "name": body["name"], "status": "new", "networks": {"v4": []}, "region": {"slug": body["region"]}, "size_slug": body["size"]})
        self.reply(202, {"droplet": {"id": 999}})


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
    """Runs the helper's main() like the command line; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin), out, err
    try:
        code = cloud.main(argv)
    finally:
        sys.stdin, sys.stdout, sys.stderr = old
    return code, out.getvalue(), err.getvalue()


print("Before anything is set up")
code, out, _ = run(["status"])
st = json.loads(out)
check(code == 0 and not st["token_set"] and st["kit"] is None and "scp" in st["next_step"], "status says what's missing (kit, then token)")
code, _, err = run(["create"], json.dumps({"confirm": True}))
check(code == 1 and "PUBLIC_HOST" in err, "can't create before the installer has set the web address")
cloud.write_env({"DO_TOKEN": "", "PUBLIC_HOST": "206-189-123-28.sslip.io"})
code, _, err = run(["create"], json.dumps({"confirm": True}))
check(code == 1 and "kit" in err.lower(), "can't create without the kit")
os.makedirs(cloud.STATE_DIR, exist_ok=True)
kit_bytes = os.urandom(300_000)
open(cloud.KIT, "wb").write(kit_bytes)
code, _, err = run(["create"], json.dumps({"confirm": True}))
check(code == 1 and "set-token" in err, "can't create without the DigitalOcean token")
cloud.write_env({"DO_TOKEN": TOKEN, "PUBLIC_HOST": "206-189-123-28.sslip.io"})
check(oct(os.stat(cloud.ENV).st_mode & 0o777) == "0o600", "token file is root-only (600)")

print("Safety")
code, _, err = run(["create"], json.dumps({}))
check(code == 1 and "confirm" in err, "creating needs confirm: true")
try:
    cloud.do("DELETE", "/droplets/999")
    check(False, "delete refused")
except cloud.Refused:
    check(True, "delete, resize or power actions can't be sent to DigitalOcean at all")
try:
    cloud.do("POST", "/droplets/999/actions", {"type": "power_off"})
    check(False, "power off refused")
except cloud.Refused:
    check(True, "droplet actions (power off, resize, rebuild) refused")
code, _, err = run(["create"], json.dumps({"confirm": True, "custom_domain": "evil.com; rm -rf /"}))
check(code == 1 and "domain" in err, "a bad custom domain is refused")

print("Create")
code, out, err = run(["create"], json.dumps({"confirm": True}))
check(code == 0 and json.loads(out)["droplet_id"] == 999, f"server created ({err.strip()})")
post = [c for c in calls if c[0] == "POST"]
check(len(post) == 1 and post[0][1] == "/v2/droplets", "exactly one create call")
body = post[0][3]
check(body["region"] == "lon1" and body["size"] == "s-1vcpu-2gb" and body["image"] == "ubuntu-24-04-x64" and body["tags"] == ["sweetspot-orders"],
      "London, 2 GB, Ubuntu 24.04, tagged sweetspot-orders")
check(body["ssh_keys"] == [11], "the account's SSH key goes on the server")
ud = body["user_data"]
kit_code = re.search(r"/cloud/kit/([0-9a-f]{48})", ud).group(1)
cb_code = re.search(r"CALLBACK_TOKEN='([0-9a-f]{48})'", ud).group(1)
check(hashlib.sha256(kit_bytes).hexdigest() in ud and "https://206-189-123-28.sslip.io/cloud/callback" in ud, "setup script has the kit checksum and report-back address")
check(TOKEN not in ud and TOKEN not in out and TOKEN not in err, "the DigitalOcean token is never in the script or the output")
check(subprocess.run(["bash", "-n"], input=ud, text=True).returncode == 0, "setup script is valid bash")
state = json.load(open(cloud.STATE))
check(kit_code not in json.dumps(state) and cb_code not in json.dumps(state), "only hashes of the one-time codes are stored")
code, _, err = run(["create"], json.dumps({"confirm": True}))
check(code == 1 and "already exists" in err, "a second server is refused")
code, out, _ = run(["status"])
check(json.loads(out)["droplets"][0]["id"] == 999 and json.loads(out)["install"]["status"] == "installing", "status shows the server installing")
check(all(c[2] == f"Bearer {TOKEN}" for c in calls), "every DigitalOcean call carries the token (and nothing else sees it)")

print("Kit download (by the new server)")
code, _, _ = run(["kit", "0" * 48])
check(code == 1, "wrong kit code refused")
code, out, _ = run(["kit", kit_code])
check(code == 0 and base64.b64decode(out) == kit_bytes, "kit downloads intact with the right code")
for _ in range(4):
    run(["kit", kit_code])
code, _, _ = run(["kit", kit_code])
check(code == 1, "kit code stops after 5 downloads")

print("Report back (by the new server)")
dom = "1-2-3-4.sslip.io"
good = {"token": cb_code, "ok": True, "ip": "1.2.3.4", "domain": dom, "setup_url": f"https://{dom}/setup#t={'c' * 48}",
        "connector_url": f"https://{dom}/mcp/{'d' * 64}"}
code, _, _ = run(["callback"], json.dumps({**good, "token": "0" * 48}))
check(code == 1, "report with a wrong code refused")
code, _, _ = run(["callback"], json.dumps(good))
check(code == 0, "report accepted")
code, out, _ = run(["status"])
inst = json.loads(out)["install"]
check(inst["status"] == "ready" and inst["setup_url"] == good["setup_url"] and inst["ip"] == "1.2.3.4", "status shows ready with the one-time setup link")
check("d" * 64 not in out and inst["new_server_connector"].startswith("ready"), "the new connector's address is kept on the server, never shown to Claude")
check(oct(os.stat(cloud.CONNECTOR_URL).st_mode & 0o777) == "0o600", "and it's root-only")
code, _, _ = run(["callback"], json.dumps(good))
check(code == 1, "the report code works only once")

print("Admin-only commands")
os.environ["SUDO_USER"] = "claude-mcp"
code, _, err = run(["connector-url"])
check(code == 1 and "Only you" in err, "the connector can't read the new server's connector address")
code, _, err = run(["set-token"])
check(code == 1, "the connector can't change the token")
del os.environ["SUDO_USER"]

print(f"\nAll {passed} cloud checks passed.")
srv.shutdown()
