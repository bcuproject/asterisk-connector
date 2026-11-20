#!/bin/sh
# Builds a throwaway fake Asterisk in /tmp/fakeast for test/e2e.mjs
set -e
rm -rf /tmp/fakeast; mkdir -p /tmp/fakeast/etc /tmp/fakeast/log /tmp/fakeast/bin
cat > /tmp/fakeast/etc/extensions.conf <<'X'
[from-internal]
exten => 100,1,Dial(PJSIP/100,20)
 same => n,Voicemail(100@default)
exten => 101,1,Dial(PJSIP/101,20)
X
printf '[transport-udp]\ntype=transport\nprotocol=udp\nbind=0.0.0.0\n' > /tmp/fakeast/etc/pjsip.conf
echo "[2026-11-20 00:00:00] NOTICE[1] Asterisk ready" > /tmp/fakeast/log/messages
cat > /tmp/fakeast/bin/asterisk <<'X'
#!/bin/sh
[ "$1" = "-rx" ] || { echo "usage" >&2; exit 1; }
case "$2" in
  "core show version") echo "Asterisk 20.9.3 built by fake";;
  "core show uptime") echo "System uptime: 3 days";;
  "core show channels count") echo "0 active calls";;
  "pjsip show endpoints") echo " Endpoint:  100   Not in use";;
  "dialplan reload")
     if grep -q BROKEN /tmp/fakeast/etc/extensions.conf; then echo "[2026-11-20 00:00:01] ERROR[2] pbx_config.c: Syntax error in extensions.conf" >> /tmp/fakeast/log/messages; fi
     echo "Dialplan reloaded.";;
  *) echo "ran: $2";;
esac
X
chmod +x /tmp/fakeast/bin/asterisk
