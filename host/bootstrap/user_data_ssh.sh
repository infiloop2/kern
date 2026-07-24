#!/usr/bin/env bash
# SSH delivery, stage 1 EC2 user data: hardens the base accounts, installs the
# single-use deploy key, and stages the provisioning payload. Stage 2 pushes
# the runtime code and bootstrap script over SSH and runs bootstrap, which
# reads the payload staged here.
set -euo pipefail
umask 077

id -u kern-operator >/dev/null 2>&1 || useradd --create-home --shell /bin/bash kern-operator
mkdir -p /home/kern-operator/.ssh
cat > /home/kern-operator/.ssh/authorized_keys2 <<'KEYS'
@DEPLOY_PUBLIC_KEY@
KEYS
chmod 700 /home/kern-operator/.ssh
chmod 600 /home/kern-operator/.ssh/authorized_keys2
chown -R kern-operator:kern-operator /home/kern-operator/.ssh

echo 'kern-operator ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/kern-operator
chmod 440 /etc/sudoers.d/kern-operator
gpasswd -d ubuntu sudo >/dev/null 2>&1 || true
rm -f /etc/sudoers.d/90-cloud-init-users

cat > /tmp/kern_payload.json <<'KERN_PAYLOAD_EOF'
@PAYLOAD_JSON@
KERN_PAYLOAD_EOF
chmod 600 /tmp/kern_payload.json
