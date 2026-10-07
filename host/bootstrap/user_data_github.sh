#!/usr/bin/env bash
# GitHub delivery: single-stage EC2 user data. Cloud-init runs this as root at
# first boot. It hardens the base accounts, stages the provisioning payload,
# fetches the pinned public commit, and hands off to
# host.bootstrap.self_provision, which renders and runs the same bootstrap the
# SSH delivery pushes. No deploy key exists in this mode; port 22 stays closed
# unless the stored operator connections include an ssh endpoint.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077

# Preserve the instance and root volume when provisioning fails so operators
# can inspect cloud-init logs and repair networking/packages before retrying.
# The lifecycle command can later recover or replace the failed host.
on_exit() {
  code=$?
  if [ "$code" != 0 ]; then
    systemctl start apt-daily.timer apt-daily-upgrade.timer || echo "warning: could not restart APT maintenance timers" >&2
    echo "Kern provisioning failed (exit $code); preserving instance for diagnosis" >&2
  fi
}
trap on_exit EXIT

id -u kern-operator >/dev/null 2>&1 || useradd --create-home --shell /bin/bash kern-operator
echo 'kern-operator ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/kern-operator
chmod 440 /etc/sudoers.d/kern-operator
gpasswd -d ubuntu sudo >/dev/null 2>&1 || true
rm -f /etc/sudoers.d/90-cloud-init-users

cat > /tmp/kern_payload.json <<'KERN_PAYLOAD_EOF'
@PAYLOAD_JSON@
KERN_PAYLOAD_EOF
chmod 600 /tmp/kern_payload.json

# The apt-daily/apt-daily-upgrade timers fire right after first boot and hold
# the apt/dpkg locks while downloading pending updates; stop them so the
# install below cannot stall behind them. The fetched bootstrap restarts them
# once its own apt work is done (it is always this same version: the CLI
# refuses a pin whose VERSION differs from its own).
systemctl stop apt-daily.timer apt-daily-upgrade.timer
systemctl stop apt-daily.service apt-daily-upgrade.service

@APT_HELPERS@

# Ubuntu AMIs normally include git. If it is missing, use the same bounded
# mirror fallback as the fetched bootstrap, before depending on GitHub.
if ! command -v git >/dev/null 2>&1; then
  apt_get update
  apt_get install --no-upgrade -y git
fi

rm -rf /tmp/kern-checkout
git init -q /tmp/kern-checkout
cd /tmp/kern-checkout
git remote add origin 'https://github.com/@GITHUB_REPOSITORY@.git'
for attempt in $(seq 1 60); do
  if git fetch -q --depth 1 origin '@COMMIT_SHA@'; then
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "could not fetch pinned Kern commit @COMMIT_SHA@" >&2
    exit 1
  fi
  sleep 30
done
git checkout -q --detach FETCH_HEAD

PYTHONPATH=/tmp/kern-checkout python3 -m host.bootstrap.self_provision \
  --payload /tmp/kern_payload.json \
  --checkout /tmp/kern-checkout
