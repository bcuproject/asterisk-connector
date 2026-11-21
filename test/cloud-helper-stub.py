"""Runs claude-cloud-helper with test folders and a canned DigitalOcean (used by test/cloud-e2e.mjs)."""
import importlib.util, json, os, sys
from urllib.parse import parse_qs, urlparse
spec = importlib.util.spec_from_file_location("cloud", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup", "claude-cloud-helper.py"))
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
d = os.environ["CLOUD_TEST_DIR"]
c.ENV, c.STATE_DIR = f"{d}/cloud.env", f"{d}/state"
c.KIT, c.STATE = f"{d}/state/kit.zip", f"{d}/state/state.json"
c.MENUS, c.CONNECTORS = f"{d}/state/menus", f"{d}/state/connectors"
DB = f"{d}/droplets.json"
def fake(method, path, body=None):
    made = json.load(open(DB)) if os.path.exists(DB) else []
    u = urlparse(path)
    if method == "GET" and u.path == "/account/keys": return {"ssh_keys": [{"id": 5, "name": "laptop"}]}
    if method == "GET" and u.path == "/droplets":
        tag = parse_qs(u.query).get("tag_name", [""])[0]
        return {"droplets": [x for x in made if tag in x["tags"]]}
    if method == "POST" and u.path == "/droplets":
        x = {"id": 77 + len(made), "name": body["name"], "tags": body["tags"], "status": "active", "networks": {"v4": []}, "region": {"slug": "lon1"}, "size_slug": body["size"]}
        json.dump(made + [x], open(DB, "w")); open(f"{d}/user_data_{body['name']}", "w").write(body["user_data"]); return {"droplet": {"id": x["id"]}}
    raise c.Refused("not allowed")
c.do = fake
sys.exit(c.main(sys.argv[1:]))
