#!/bin/sh
# One-command install of the connector ON the server itself (no SSH, no Docker, no Lovable):
# Asterisk configs/CLI, plus the voice-agent app folders (e.g. /opt/bcu-livekit) and their services.
#   sudo sh setup/install-on-asterisk-server.sh [your-domain]
# Without a domain it uses <server-ip>.sslip.io, which points at this server, for a free HTTPS certificate.
# Safe to run again: it updates the code and keeps the existing token.
set -eu
[ "$(id -u)" = 0 ] || { echo "Please run with sudo."; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/asterisk-connector
ENVF=/etc/asterisk-connector.env
SVC_USER=claude-mcp
PORT=8787
AST_BIN="$(command -v asterisk || echo /usr/sbin/asterisk)"
if [ -x "$AST_BIN" ] && [ -d /etc/asterisk ]; then HAS_AST=1; AST_GROUP="$(stat -c %G /etc/asterisk)"
else HAS_AST=0; AST_GROUP=nogroup; getent group nogroup >/dev/null || AST_GROUP=nobody; echo "Asterisk not found on this machine: installing the app tools only."; fi
ARCH="$(uname -m)"; case "$ARCH" in x86_64) NA=x64; CA=amd64;; aarch64|arm64) NA=arm64; CA=arm64;; *) echo "Unsupported CPU $ARCH"; exit 1;; esac
say() { printf '\n== %s\n' "$*"; }

say "Service user"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
[ "$HAS_AST" = 1 ] && usermod -aG "$AST_GROUP" "$SVC_USER"

if [ "$HAS_AST" = 1 ]; then
say "Permissions (config read/write via the $AST_GROUP group, logs readable)"
chgrp -R "$AST_GROUP" /etc/asterisk
chmod -R g+rwX /etc/asterisk
find /etc/asterisk -type d -exec chmod g+s {} +
mkdir -p /etc/asterisk/.claude-backups; chgrp "$AST_GROUP" /etc/asterisk/.claude-backups; chmod 2770 /etc/asterisk/.claude-backups
[ -d /var/log/asterisk ] && { chgrp -R "$AST_GROUP" /var/log/asterisk; chmod -R g+rX /var/log/asterisk; } || true
fi

say "sudo rule: $SVC_USER may run ONLY '$AST_BIN -rx ...' and the claude-app-helper"
CLOUD="${CLOUD:-$HAS_AST}"   # creating the Sweet Spot server on DigitalOcean: on the phone server only (CLOUD=0 to skip)
if [ "$HAS_AST" = 1 ]; then ALLOWED="$AST_BIN -rx *, /usr/local/sbin/claude-app-helper *"; else ALLOWED="/usr/local/sbin/claude-app-helper *"; fi
[ "$CLOUD" = 1 ] && ALLOWED="$ALLOWED, /usr/local/sbin/claude-cloud-helper *"
printf 'Defaults:%s !requiretty\n%s ALL=(root) NOPASSWD: %s\n' "$SVC_USER" "$SVC_USER" "$ALLOWED" > /etc/sudoers.d/claude-mcp
chmod 440 /etc/sudoers.d/claude-mcp; visudo -cf /etc/sudoers.d/claude-mcp >/dev/null

if [ "$CLOUD" = 1 ]; then
  say "Sweet Spot server creation through claude-cloud-helper (it alone holds the DigitalOcean token)"
  install -o root -g root -m 755 "$SRC/setup/claude-cloud-helper.py" /usr/local/sbin/claude-cloud-helper
  install -d -o root -g root -m 700 /var/lib/claude-cloud
fi

say "App access (voice-agent folders and services) through claude-app-helper"
install -o root -g root -m 755 "$SRC/setup/claude-app-helper.py" /usr/local/sbin/claude-app-helper
HCONF=/etc/claude-app-helper.conf
if [ -f "$HCONF" ]; then
  echo "Keeping existing $HCONF (edit it to change which folders/services Claude may use)."
else
  PAT='livekit|sweetspot|taxi|bcu|phone-server|aggs|kks|ada|voice|agent'
  UNITS="$( { systemctl list-unit-files --type=service --no-legend 2>/dev/null; systemctl list-units --all --type=service --no-legend 2>/dev/null; } \
    | awk '{for(i=1;i<=NF;i++) if ($i ~ /\.service$/) {print $i; break}}' | grep -Ei "$PAT" | grep -v -e asterisk-connector -e caddy-connector | sort -u | paste -sd, -)"
  DIRS="/opt/bcu-livekit"
  PY=""
  for u in $(echo "$UNITS" | tr , ' '); do
    wd="$(systemctl show "$u" -p WorkingDirectory --value 2>/dev/null)"
    case "$wd" in /opt/*|/srv/*) DIRS="$DIRS,$wd";; esac
    ex="$(systemctl show "$u" -p ExecStart --value 2>/dev/null | sed -n 's/.*path=\([^ ;]*\).*/\1/p')"
    case "$ex" in *python*) [ -z "$PY" ] && [ -x "$ex" ] && PY="$ex";; esac
  done
  DIRS="$(echo "$DIRS" | tr , '\n' | awk 'NF && !seen[$0]++' | while read -r d; do [ -d "$d" ] && echo "$d"; done | paste -sd, -)"
  cat > "$HCONF" <<EOF
# Which folders and services Claude may use through the connector. Root-owned: only an admin can change this.
# Keys, .env files, certificates and call recordings inside these folders are never readable.
dirs = ${DIRS}
units = ${UNITS}
backup_dir = /var/backups/claude-app
python = ${PY:-$(command -v python3)}
EOF
  chown root:root "$HCONF"; chmod 644 "$HCONF"
fi
echo "Folders:  $(sed -n 's/^dirs = //p' "$HCONF")"
echo "Services: $(sed -n 's/^units = //p' "$HCONF")"

say "Node.js"
NODE=""
for n in /opt/node22/bin/node "$(command -v node || true)"; do
  [ -n "$n" ] && [ -x "$n" ] && [ "$("$n" -p 'process.versions.node.split(".")[0]')" -ge 20 ] 2>/dev/null && { NODE="$n"; break; }
done
if [ -z "$NODE" ]; then
  F="$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt | awk -v a="linux-$NA.tar.xz" '$2 ~ a"$" {print $2; exit}')"
  [ -n "$F" ] || { echo "Could not find a Node.js download"; exit 1; }
  curl -fsSL "https://nodejs.org/dist/latest-v22.x/$F" -o /tmp/node.tar.xz
  curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt | grep " $F\$" | sed "s| $F\$| /tmp/node.tar.xz|" | sha256sum -c -
  rm -rf /opt/node22 && mkdir -p /opt/node22 && tar -xJf /tmp/node.tar.xz -C /opt/node22 --strip-components=1 && rm /tmp/node.tar.xz
  NODE=/opt/node22/bin/node
fi
NPM="$(dirname "$NODE")/npm"; [ -x "$NPM" ] || NPM="$(command -v npm)"
echo "Using $NODE ($("$NODE" -v))"

say "Install app to $APP"
mkdir -p "$APP"
rm -rf "$APP/src"; cp -r "$SRC/package.json" "$SRC/package-lock.json" "$SRC/tsconfig.json" "$SRC/src" "$APP/"
cd "$APP"
PATH="$(dirname "$NODE"):$PATH" "$NPM" ci --no-audit --no-fund --loglevel=error
PATH="$(dirname "$NODE"):$PATH" "$NODE" node_modules/typescript/bin/tsc
PATH="$(dirname "$NODE"):$PATH" "$NPM" prune --omit=dev --no-audit --no-fund --loglevel=error
chown -R root:root "$APP"; chmod -R a+rX "$APP"

say "Settings ($ENVF)"
if [ -f "$ENVF" ] && grep -q '^CONNECTOR_TOKEN=.\{32,\}' "$ENVF"; then
  echo "Keeping existing token."
else
  TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > "$ENVF" <<EOF
CONNECTOR_TOKEN=$TOKEN
RUN_MODE=local
BIND=127.0.0.1
PORT=$PORT
ASTERISK_CMD=sudo -n $AST_BIN
ASTERISK_CONF_DIR=/etc/asterisk
ASTERISK_LOG_DIR=/var/log/asterisk
ALLOW_WRITE=true
APP_HELPER_CMD=sudo -n /usr/local/sbin/claude-app-helper
ASTERISK_TOOLS=$([ "$HAS_AST" = 1 ] && echo true || echo false)
EOF
fi
grep -q '^APP_HELPER_CMD=' "$ENVF" || echo 'APP_HELPER_CMD=sudo -n /usr/local/sbin/claude-app-helper' >> "$ENVF"
if [ "$CLOUD" = 1 ]; then grep -q '^CLOUD_HELPER_CMD=' "$ENVF" || echo 'CLOUD_HELPER_CMD=sudo -n /usr/local/sbin/claude-cloud-helper' >> "$ENVF"
else sed -i '/^CLOUD_HELPER_CMD=/d' "$ENVF"; fi
chown root:"$SVC_USER" "$ENVF"; chmod 640 "$ENVF"

say "Service asterisk-connector"
cat > /etc/systemd/system/asterisk-connector.service <<EOF
[Unit]
Description=Asterisk connector for Claude (MCP)
After=network-online.target

[Service]
User=$SVC_USER
Group=$AST_GROUP
EnvironmentFile=$ENVF
ExecStart=$NODE $APP/dist/server.js
Restart=always
RestartSec=3
UMask=0007
PrivateTmp=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now asterisk-connector >/dev/null
systemctl restart asterisk-connector
sleep 2
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null && echo "Connector is running on 127.0.0.1:$PORT" || { journalctl -u asterisk-connector -n 30 --no-pager; exit 1; }

say "HTTPS"
IP="$(curl -fsS -4 --max-time 5 https://ifconfig.me || hostname -I | awk '{print $1}')"
OLDHOST="$(sed -n '1s/ *{.*//p' /etc/caddy-connector/Caddyfile 2>/dev/null)"
HOST="${1:-${OLDHOST:-$(echo "$IP" | tr . -).sslip.io}}"
if [ "$CLOUD" = 1 ]; then
  CE=/etc/claude-cloud.env
  [ -f "$CE" ] || ( umask 077; printf '# DigitalOcean access for claude-cloud-helper. Root-only. Never paste the token into chat.\nDO_TOKEN=\n' > "$CE" )
  sed -i '/^PUBLIC_HOST=/d' "$CE"; echo "PUBLIC_HOST=$HOST" >> "$CE"
  grep -q '^UI_KEY=.\{32,\}' "$CE" || echo "UI_KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$CE"
  chown root:root "$CE"; chmod 600 "$CE"
fi
busy() { (ss -ltnH "sport = :$1" 2>/dev/null || netstat -ltn 2>/dev/null | grep ":$1 ") | grep -q .; }
if systemctl is-active --quiet caddy-connector; then
  if ! grep -q '/cloud/\*' /etc/caddy-connector/Caddyfile; then
    sed -i 's#@mcp path /mcp /mcp/\* /health#@mcp path /mcp /mcp/* /health /cloud/*#' /etc/caddy-connector/Caddyfile
    systemctl restart caddy-connector
  fi
  echo "Caddy (connector) already set up."
elif busy 443 || busy 80; then
  echo "Ports 80/443 are already used by another web server on this machine:"
  (ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep -E ':(80|443)\s' || true
  echo "Add this to that web server instead (nginx example), inside the server block for $HOST:"
  echo "    location /mcp/ { proxy_pass http://127.0.0.1:$PORT; proxy_http_version 1.1; proxy_buffering off; }"
  HTTPS_NOTE="(set up the proxy above first)"
else
  if [ ! -x /usr/local/bin/caddy ]; then
    curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$CA" -o /usr/local/bin/caddy
    chmod 755 /usr/local/bin/caddy
  fi
  id caddy >/dev/null 2>&1 || useradd --system --home-dir /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
  mkdir -p /etc/caddy-connector
  cat > /etc/caddy-connector/Caddyfile <<EOF
$HOST {
	@mcp path /mcp /mcp/* /health /cloud/*
	handle @mcp {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond 404
	}
}
EOF
  cat > /etc/systemd/system/caddy-connector.service <<EOF
[Unit]
Description=Caddy HTTPS for the Asterisk connector
After=network-online.target

[Service]
User=caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy-connector/Caddyfile
Restart=always
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=XDG_DATA_HOME=/var/lib/caddy XDG_CONFIG_HOME=/var/lib/caddy

[Install]
WantedBy=multi-user.target
EOF
  command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active" && { ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; echo "Opened ports 80 and 443 in ufw."; }
  systemctl daemon-reload; systemctl enable --now caddy-connector >/dev/null
  echo "Getting a certificate for $HOST (can take up to a minute)…"
  for i in $(seq 1 30); do curl -fsS --max-time 5 "https://$HOST/health" >/dev/null 2>&1 && { echo "HTTPS works."; break; }; sleep 3; done
fi

TOKEN="$(sed -n 's/^CONNECTOR_TOKEN=//p' "$ENVF")"
UI_LINE=""
if [ "$CLOUD" = 1 ]; then
  UI_LINE="
 Create the Sweet Spot server (private page; keep this link to yourself):
   https://$HOST/cloud/ui#k=$(sed -n 's/^UI_KEY=//p' /etc/claude-cloud.env)"
fi
cat <<EOF

=====================================================================
 Done. In Claude: Settings -> Connectors -> Add custom connector
   Name: BCU Server
   URL:  https://$HOST/mcp/$TOKEN  ${HTTPS_NOTE:-}

 Keep this URL private: the long code in it is the password.
 Logs:      journalctl -u asterisk-connector -f
 Read-only: set ALLOW_WRITE=false in $ENVF, then: systemctl restart asterisk-connector
 App folders/services Claude may use: /etc/claude-app-helper.conf
${UI_LINE:-}
 Remove:    systemctl disable --now asterisk-connector caddy-connector
=====================================================================
EOF
