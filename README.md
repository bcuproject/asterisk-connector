# Asterisk Connector for Claude

A custom connector (remote MCP server) that lets Claude look after your Asterisk PBX for you. It connects to the server over SSH.

**What Claude can do through it**

| Tool | What it does |
|---|---|
| `server_status` | Shows the Asterisk version, uptime and active calls |
| `cli` | Runs **read-only** CLI commands: `pjsip show endpoints`, `core show channels`, `dialplan show`, `queue show`, `sip show peers`, `database show`… |
| `list_configs` / `read_config` | Lists and reads files in `/etc/asterisk` |
| `edit_config` | Replaces an exact piece of text in a config file (see "What happens on every change") |
| `write_config` | Creates a new config file, or replaces a whole one if Claude passes the hash of the version it read |
| `reload` | Reloads `dialplan`, `pjsip`, `sip`, `iax2`, `voicemail`, `queues`, `features`, `musiconhold`, `confbridge`, `manager`, `logger`, `rtp`, `cdr` or `all` |
| `list_backups` / `restore_backup` | Undoes a change |
| `tail_log` | Reads `/var/log/asterisk/messages` (or `full`, `queue_log`…), with an optional text filter |

**What happens on every change**
1. The current file is backed up to `/etc/asterisk/.claude-backups/`.
2. The new version is written in one step. A half-written file is never left behind.
3. Claude gets a diff showing exactly what changed.
4. The matching module is reloaded, and any new `WARNING`/`ERROR` lines in the log are reported.
5. **If the reload logs an `ERROR`, the old file is put back and reloaded automatically.**

**Safety**
- The connector has no raw shell. It can't run `channel request hangup`, `originate`, `core stop` or any other CLI command that changes things.
- File names are restricted to `*.conf`, `*.ael`, `*.lua` and `*.txt` inside `/etc/asterisk`, so paths like `../` are rejected.
- The SSH server key is pinned, and the connector refuses to connect if it changes.
- On the server, the SSH user `claude-mcp` has no login prompt and no port forwarding. Its sudo access covers only `asterisk -rx …`.
- Every tool call is logged as one JSON line. Config contents are never logged.
- `ALLOW_WRITE=false` turns the connector into a read-only one.

---

## Quickest setup: install on the Asterisk server itself (one command)
Log in to the server (PuTTY or a terminal) and paste:
```sh
cd /tmp && curl -fsSL https://github.com/bcuproject/asterisk-connector/archive/refs/heads/main.tar.gz | tar xz && sudo sh asterisk-connector-main/setup/install-on-asterisk-server.sh
```
The script:
- creates the locked-down `claude-mcp` user
- installs Node.js if it isn't there
- starts the connector as a service on `127.0.0.1` only
- puts HTTPS in front of it with Caddy, using `<server-ip>.sslip.io` (free, no domain needed)
- prints the URL to add in Claude

If ports 80/443 are already used by another web server, it prints a proxy snippet to add to that server instead. Run it again any time to update the connector; your token is kept.

## Setup on a separate machine (Docker + SSH)

You need:
- A machine with Docker that is reachable from the internet. A small VPS is fine, and so is the PBX itself.
- A domain name pointing at that machine, e.g. `pbx-connector.yourdomain.com`.

### 1. Make an SSH key for the connector (on the connector machine)
```sh
git clone https://github.com/<you>/asterisk-connector && cd asterisk-connector
mkdir -p keys && ssh-keygen -t ed25519 -N "" -C asterisk-connector -f keys/id_ed25519
chmod 644 keys/id_ed25519   # the container runs as a non-root user and must be able to read it
cat keys/id_ed25519.pub     # copy this line
```

### 2. Prepare the Asterisk server (once, as root)
```sh
sudo sh setup/asterisk-server-setup.sh "ssh-ed25519 AAAA... asterisk-connector" <connector-ip>
```
The script creates the locked-down `claude-mcp` user, sets the permissions and prints three values for the next step. The `<connector-ip>` is optional; it locks the key so it only works from that address.

### 3. Configure and start the connector
```sh
cp .env.example .env
openssl rand -hex 32        # paste as CONNECTOR_TOKEN
nano .env                   # fill in DOMAIN, ASTERISK_SSH_HOST and the values printed in step 2
docker compose up -d
docker compose logs -f connector
```
Caddy gets an HTTPS certificate for your domain automatically, so ports 80 and 443 must be open.

### 4. Add it to Claude
In Claude, go to **Settings → Connectors → Add custom connector** and enter:
- **Name:** Asterisk PBX
- **URL:** `https://pbx-connector.yourdomain.com/mcp/<CONNECTOR_TOKEN>`

Then try asking:
- "Show me which PJSIP endpoints are registered"
- "Change extension 100 to ring for 30 seconds before voicemail"
- "Why is trunk X failing? Check the log"

> The token in the URL is the password. Keep it private, and change `CONNECTOR_TOKEN` (and update the connector URL) if it is ever exposed.

### Running on the Asterisk server itself
Set `RUN_MODE=local` and run it as the `claude-mcp` user, for example with Node 20+:
```sh
npm ci && npm run build && BIND=127.0.0.1 node dist/server.js
```
Put Caddy or nginx in front of it for HTTPS. In this mode no SSH is needed.

## Development
```sh
npm ci && npm run build && npm test   # tests run against a fake Asterisk, both locally and over a real SSH connection
```


## App tools: voice agents on the same server (v1.1)

The installer also sets up access to the voice-agent apps (e.g. `/opt/bcu-livekit`, the LiveKit / Sweet Spot agent) so Claude can check what is really running and fix it:

| Tool | What it does |
|---|---|
| `server_services` | State, start time, command and folder of each allowed service |
| `service_logs` | Recent journal lines for a service, optionally filtered (secrets masked) |
| `restart_service` | Restart an allowed service and report its state + latest logs (drops live calls on it) |
| `list_app_files` / `read_app_file` | Browse and read code/config in the allowed folders (secret values shown as «hidden») |
| `edit_app_file` / `write_app_file` | Exact-text edits or new files; backed up first; Python must compile or nothing is saved |
| `list_app_backups` / `restore_app_file` | Undo any change |

All of it goes through one root-owned script, `/usr/local/sbin/claude-app-helper`, which enforces the rules itself:

* Only the folders and services listed in `/etc/claude-app-helper.conf` (root-owned; the installer fills it in from the services it finds; edit it to add or remove).
* Never readable: `.env` files, keys and certificates, databases, call recordings, `.git`, virtualenvs.
* Secret values in readable files and logs are masked; Claude can't add secrets to files, and a file that contains secrets can only be edited (secrets kept), never replaced.
* The connector user's sudo rule allows only `asterisk -rx …` and this helper.

If Asterisk isn't on the machine, the installer sets up the app tools only.
