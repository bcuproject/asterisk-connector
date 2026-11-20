#!/bin/sh
# Runs the e2e suite through a real SSH connection (local test SSH server, pinned host key).
set -e
D=$(mktemp -d)
node -e "
const {utils}=require('ssh2');const fs=require('fs');const c=require('crypto');
for(const n of ['host','client']){const k=utils.generateKeyPairSync('ed25519');fs.writeFileSync('$D/'+n,k.private,{mode:0o600});fs.writeFileSync('$D/'+n+'.pub',k.public)}
const pub=utils.parseKey(fs.readFileSync('$D/host.pub'));fs.writeFileSync('$D/fp','SHA256:'+c.createHash('sha256').update(pub.getPublicSSH()).digest('base64').replace(/=+$/,''));"
node test/ssh-test-server.mjs "$D/host" "$D/client.pub" 2222 & S=$!
sleep 1
RUN_MODE=ssh ASTERISK_SSH_HOST=127.0.0.1 ASTERISK_SSH_PORT=2222 ASTERISK_SSH_USER=test ASTERISK_SSH_KEY="$D/client" ASTERISK_HOST_FINGERPRINT="$(cat "$D/fp")" node test/e2e.mjs; R=$?
kill $S; rm -rf "$D"; exit $R
