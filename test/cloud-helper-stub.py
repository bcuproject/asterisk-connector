"""Runs claude-cloud-helper with test folders and a canned DigitalOcean (used by test/cloud-e2e.mjs)."""
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("cloud", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "setup", "claude-cloud-helper.py"))
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
d = os.environ["CLOUD_TEST_DIR"]
c.ENV, c.STATE_DIR = f"{d}/cloud.env", f"{d}/state"
c.KIT, c.STATE, c.CONNECTOR_URL = f"{d}/state/sweetspot-system.zip", f"{d}/state/state.json", f"{d}/state/conn.txt"
made = os.path.exists(f"{d}/made")
def fake(method, path, body=None):
    if method == "GET" and path.startswith("/account/keys"): return {"ssh_keys": [{"id": 5, "name": "laptop"}]}
    if method == "GET" and path.startswith("/droplets"):
        return {"droplets": [{"id": 77, "name": "sweetspot-orders", "status": "active", "networks": {"v4": []}, "region": {"slug": "lon1"}, "size_slug": "s-1vcpu-2gb"}] if made else []}
    if method == "POST" and path == "/droplets":
        open(f"{d}/made", "w").write("1"); open(f"{d}/user_data", "w").write(body["user_data"]); return {"droplet": {"id": 77}}
    raise c.Refused("not allowed")
c.do = fake
sys.exit(c.main(sys.argv[1:]))
