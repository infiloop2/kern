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

# Failed provisioning leaves no instance, same as the SSH delivery where the
# CLI terminates it: instances launch with instance-initiated shutdown
# behavior set to terminate, so shutting down on any failure terminates this
# instance and deletes its root volume. Attached data volumes survive.
on_exit() {
  code=$?
  if [ "$code" != 0 ]; then
    echo "Kern provisioning failed (exit $code); shutting down to terminate this instance" >&2
    shutdown -h now
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

# The lifecycle CLI already proved the pinned commit exists and is readable
# before launching this instance, so failures below are transient network or
# GitHub availability issues. Retry for an extended window (roughly half an
# hour each) so an outage delays provisioning instead of failing it.
for attempt in $(seq 1 60); do
  if apt-get -q -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 -o Acquire::Languages=none update \
    && apt-get -q -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 -o Acquire::Languages=none install -y git; then
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "could not install git for Kern provisioning" >&2
    exit 1
  fi
  sleep 20
done

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
