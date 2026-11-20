#!/bin/sh
# Run ONCE on your Asterisk server as root:  sudo sh asterisk-server-setup.sh "<connector public key>" [connector-ip]
# Creates a locked-down user "claude-mcp" that can:
#   - read/write files in /etc/asterisk (via the asterisk group)
#   - read /var/log/asterisk
#   - run ONLY "asterisk -rx ..." as root (sudo), nothing else
# It cannot get a shell prompt, forward ports or use sudo for anything else.
set -eu
PUBKEY="${1:?Usage: sudo sh asterisk-server-setup.sh \"ssh-ed25519 AAAA... asterisk-connector\" [connector-ip]}"
FROM_IP="${2:-}"
USER_NAME=claude-mcp
AST_BIN="$(command -v asterisk || echo /usr/sbin/asterisk)"
AST_GROUP="$(stat -c %G /etc/asterisk 2>/dev/null || echo asterisk)"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)."; exit 1; }
[ -x "$AST_BIN" ] || { echo "asterisk binary not found"; exit 1; }

id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/sh "$USER_NAME"
usermod -aG "$AST_GROUP" "$USER_NAME"

# Config dir: group-writable so edits keep their owner/group; backups folder for the connector
chgrp -R "$AST_GROUP" /etc/asterisk
chmod -R g+rwX /etc/asterisk
find /etc/asterisk -type d -exec chmod g+s {} +
mkdir -p /etc/asterisk/.claude-backups && chgrp "$AST_GROUP" /etc/asterisk/.claude-backups && chmod 2770 /etc/asterisk/.claude-backups

# Logs readable
[ -d /var/log/asterisk ] && chgrp -R "$AST_GROUP" /var/log/asterisk && chmod -R g+rX /var/log/asterisk || true

# sudo: only "asterisk -rx <anything>"; the connector itself only sends read-only commands and reloads
cat > /etc/sudoers.d/claude-mcp <<EOF
Defaults:$USER_NAME !requiretty
$USER_NAME ALL=(root) NOPASSWD: $AST_BIN -rx *
EOF
chmod 440 /etc/sudoers.d/claude-mcp
visudo -cf /etc/sudoers.d/claude-mcp >/dev/null

# SSH key with restrictions
H="$(getent passwd "$USER_NAME" | cut -d: -f6)"
mkdir -p "$H/.ssh"; chmod 700 "$H/.ssh"
OPTS="no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty"
[ -n "$FROM_IP" ] && OPTS="from=\"$FROM_IP\",$OPTS"
grep -qF "$PUBKEY" "$H/.ssh/authorized_keys" 2>/dev/null || echo "$OPTS $PUBKEY" >> "$H/.ssh/authorized_keys"
chmod 600 "$H/.ssh/authorized_keys"; chown -R "$USER_NAME": "$H/.ssh"

# If sshd only allows certain users, add ours
if grep -qE '^\s*AllowUsers' /etc/ssh/sshd_config && ! grep -qE "^\s*AllowUsers.*\b$USER_NAME\b" /etc/ssh/sshd_config; then
  sed -i -E "s/^(\s*AllowUsers.*)$/\1 $USER_NAME/" /etc/ssh/sshd_config && (systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null || true)
fi

echo
echo "Done. Put these in the connector's .env:"
echo "  ASTERISK_SSH_USER=$USER_NAME"
echo "  ASTERISK_CMD=sudo -n $AST_BIN"
echo "  ASTERISK_HOST_FINGERPRINT=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | awk '{print $2}')"
echo
echo "Quick check (from the connector machine):"
echo "  ssh -i <key> $USER_NAME@<this-server> sudo -n $AST_BIN -rx 'core show version'"
