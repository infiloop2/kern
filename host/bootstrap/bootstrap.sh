#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 077
# Bootstrap is usually launched from the operator home over SSH. Use a neutral
# cwd so runuser children do not inherit an unreadable directory.
cd /
NODE_VERSION=22.12.0
CODEX_CLI_VERSION=0.144.0
CLAUDE_CODE_VERSION=2.1.258
# Grok Build, xAI's coding agent. The npm package is a JS trampoline plus a
# per-platform optional dependency carrying a brotli-compressed binary; see
# docs/architecture/xai-integration.md for the upgrade review checklist.
GROK_CLI_VERSION=1.0.5
HERMES_AGENT_VERSION=0.18.2
FASTEMBED_VERSION=0.8.0
PGVECTOR_DEB_VERSION=0.8.6-1.pgdg22.04+1
PGVECTOR_DEB_SHA256_AMD64=a6021797a2363c134abc12282440d49d09f7f729798d56e51e8b1e92b368416f
PGVECTOR_DEB_SHA256_ARM64=c4f0ef61a366a42e6a3663d2a84edbeea6c11a13b76c304e6fc2ae22c6ded920
# The embedding model ships as a Kern release asset rather than a HuggingFace
# download. Upstream is Qdrant/bge-small-en-v1.5-onnx-Q at revision
# 52398278842ec682c6f32300af41344b1c0b0bb2, an int8-quantized ONNX conversion
# of BAAI/bge-small-en-v1.5. Refreshing the model is a deliberate, reviewed
# change: cut a new release tag and move these digests in the same commit.
EMBEDDING_MODEL_TAG=model-bge-small-en-v1.5-onnx-Q-1
EMBEDDING_MODEL_DIR=/usr/local/share/kern-embedding-models/bge-small-en-v1.5-onnx-Q
EMBEDDING_MODEL_SHA256="\
51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431  model_optimized.onnx
d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66  tokenizer.json
0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53  tokenizer_config.json
13582bcf2effc85b7bf3d3f5532e686bc1c9ce86bb009d10f0ec33cbe92299dd  config.json
5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a  special_tokens_map.json"
# hermes-agent requires Python 3.11-3.13; the base image ships 3.10, so uv
# provisions a standalone interpreter for its dedicated venv.
UV_VERSION=0.9.26
HERMES_PYTHON_VERSION=3.12
CLOUDFLARED_VERSION=2026.6.1
# Ubuntu 22.04 ships PostgreSQL 14. The data directory below is versioned by
# major so a future base-image bump gets an explicit pg_upgrade step instead
# of a silent mismatch.
PG_MAJOR=14
# Service accounts with pinned ids (rendered from host.constants
# SERVICE_ACCOUNTS, which host.bootstrap.verify_deploy re-checks against the
# live /etc/passwd at the end of this script).
@SERVICE_ACCOUNT_CONSTANTS@
PROXY_PORT=@PROXY_PORT@
WORKSPACE_PORT=@WORKSPACE_PORT@

# Persistent volume layout. The admin volume is durable across redeploys, so
# the admin-state Postgres data directory and proxy-owned mutable state live
# in separate directories with separate Unix owners.
ADMIN_MOUNT=/mnt/kern-admin
PGDATA_DIR="/mnt/kern-admin/postgres/${PG_MAJOR}/main"
PROXY_STATE_DIR=/mnt/kern-admin/proxy-state
AGENT_MOUNT=/mnt/kern-agent
AGENT_HOME_PATH=/mnt/kern-agent/agent-home
VENDOR_SOURCE_DIR=/opt/kern-host/host/bootstrap/vendor

# Read one value out of the JSON payload staged by the deploy command.
payload_value() {
  local key="$1"
  python3 - "$key" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path('/tmp/kern_payload.json').read_text())
value = payload
for part in sys.argv[1].split("."):
    value = value[part]
print(value)
PY
}

# Provider adapters resolve their attached storage to two device roles before
# this shared bootstrap formats or mounts anything.
resolve_storage_devices() (
  resolver_root="$(mktemp -d)"
  trap 'rm -rf -- "$resolver_root"' EXIT
  tar -xzf /tmp/kern-host-code.tar.gz -C "$resolver_root" \
    host/__init__.py \
    host/bootstrap
  PYTHONPATH="$resolver_root" python3 -m host.bootstrap.storage_resolver
)

# Format a new durable device exactly once, then mount it through /etc/fstab.
# Mounting by filesystem UUID keeps later guest boots independent of device
# ordering on every provider.
prepare_volume() {
  local device="$1"
  local mount_point="$2"
  local label="$3"
  local allow_format="$4"
  local existing_label uuid
  if ! blkid "$device" >/dev/null 2>&1; then
    if [ "$allow_format" != yes ]; then
      echo "preserved device ${device} has no filesystem; refusing to format it during replacement" >&2
      return 1
    fi
    mkfs.ext4 -F -L "$label" "$device"
  else
    # A role hint on the filesystem itself: refuse a device whose existing
    # label belongs to another role or another system entirely.
    existing_label="$(blkid -s LABEL -o value "$device" 2>/dev/null || true)"
    if [ -n "$existing_label" ] && [ "$existing_label" != "$label" ]; then
      echo "device ${device} has filesystem label ${existing_label}, expected ${label}; refusing to mount" >&2
      return 1
    fi
  fi
  uuid="$(blkid -s UUID -o value "$device")"
  mkdir -p "$mount_point"
  if ! grep -qE "[[:space:]]${mount_point}[[:space:]]" /etc/fstab; then
    echo "UUID=${uuid} ${mount_point} ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
  mountpoint -q "$mount_point" || mount "$mount_point"
}

# Mount durable admin and agent volumes before creating service users; their
# home directories live on those volumes. Provider adapters return exactly two
# device paths; this bootstrap knows only their admin and agent roles.
mount_durable_volumes() {
operation_mode="$(payload_value operation.mode)"
resolver_output="$(mktemp)"
if ! resolve_storage_devices > "$resolver_output"; then
  rm -f "$resolver_output"
  return 1
fi
mapfile -t role_devices < "$resolver_output"
rm -f "$resolver_output"
if [ "${#role_devices[@]}" -ne 2 ] || [ "${role_devices[0]}" = "${role_devices[1]}" ]; then
  echo "storage resolver did not return two distinct role devices" >&2
  exit 1
fi
admin_device="${role_devices[0]}"
agent_device="${role_devices[1]}"
if [ "$operation_mode" = deploy ]; then
  prepare_volume "$admin_device" "$ADMIN_MOUNT" KERN_ADMIN yes
  prepare_volume "$agent_device" "$AGENT_MOUNT" KERN_AGENT yes
else
  prepare_volume "$admin_device" "$ADMIN_MOUNT" KERN_ADMIN no
  prepare_volume "$agent_device" "$AGENT_MOUNT" KERN_AGENT no
fi
}

# Stable IDs keep durable filesystem owners meaningful across disposable
# compute replacement. If an image already uses one of these IDs, fail instead of
# silently assigning preserved data to the wrong service account.
ensure_group() {
  local name="$1"
  local gid="$2"
  local existing
  existing="$(getent group "$name" | cut -d: -f3 || true)"
  if [ -n "$existing" ]; then
    if [ "$existing" != "$gid" ]; then
      echo "group ${name} has gid ${existing}, expected ${gid}" >&2
      exit 1
    fi
    return
  fi
  if getent group "$gid" >/dev/null; then
    echo "gid ${gid} is already allocated before creating ${name}" >&2
    exit 1
  fi
  groupadd --gid "$gid" "$name"
}

ensure_user() {
  local name="$1"
  local uid="$2"
  local group="$3"
  local home="$4"
  local extra_args="${5:-}"
  local existing
  existing="$(id -u "$name" 2>/dev/null || true)"
  if [ -n "$existing" ]; then
    if [ "$existing" != "$uid" ]; then
      echo "user ${name} has uid ${existing}, expected ${uid}" >&2
      exit 1
    fi
    return
  fi
  if getent passwd "$uid" >/dev/null; then
    echo "uid ${uid} is already allocated before creating ${name}" >&2
    exit 1
  fi
  # shellcheck disable=SC2086
  useradd --uid "$uid" --gid "$group" $extra_args --home-dir "$home" --no-create-home --shell /usr/sbin/nologin "$name"
}

ensure_group_member() {
  local user="$1"
  local group="$2"
  if ! id -nG "$user" | tr ' ' '\n' | grep -Fxq "$group"; then
    usermod --append --groups "$group" "$user"
  fi
}

provision_service_accounts() {
ensure_group kern-admin "$KERN_ADMIN_GID"
ensure_group kern-proxy "$KERN_PROXY_GID"
ensure_group kern-agent "$KERN_AGENT_GID"
ensure_group cloudflared "$CLOUDFLARED_GID"
ensure_group kern-tools "$KERN_TOOLS_GID"
ensure_group kern-agent-network "$KERN_AGENT_NETWORK_GID"
ensure_group kern-workspace-api "$KERN_WORKSPACE_API_GID"
ensure_group kern-workspace "$KERN_WORKSPACE_GID"
ensure_group kern-embedding "$KERN_EMBEDDING_GID"
ensure_user kern-admin "$KERN_ADMIN_UID" kern-admin /mnt/kern-admin/admin-home
ensure_user kern-proxy "$KERN_PROXY_UID" kern-proxy /mnt/kern-admin/proxy-state
ensure_user kern-agent "$KERN_AGENT_UID" kern-agent /mnt/kern-agent/agent-home
ensure_user cloudflared "$CLOUDFLARED_UID" cloudflared /nonexistent
# The tools service holds no durable state of its own (its state lives in the
# tool tables, reached with a scoped Postgres role), so it needs no home.
ensure_user kern-tools "$KERN_TOOLS_UID" kern-tools /nonexistent
# The agent-network service serves read-only policy introspection with no
# filesystem state or egress.
ensure_user kern-agent-network "$KERN_AGENT_NETWORK_UID" kern-agent-network /nonexistent
ensure_user kern-workspace "$KERN_WORKSPACE_UID" kern-workspace /nonexistent
ensure_user kern-embedding "$KERN_EMBEDDING_UID" kern-embedding /nonexistent
ensure_group_member kern-admin kern-workspace-api
ensure_group_member kern-workspace kern-workspace-api
# The postgres account is created here, before the postgresql packages would
# create it with a dynamic system uid: the preserved cluster files on the
# admin volume are 0600/0700 postgres-owned, so like the kern-* users
# the uid must stay stable across root-volume replacement. The Debian
# packaging reuses an existing postgres user as-is.
ensure_group postgres "$POSTGRES_GID"
ensure_user postgres "$POSTGRES_UID" postgres /var/lib/postgresql
}

# Sanitize managed paths on reused durable volumes before root writes anything
# there. Symlinks planted by a compromised previous service are removed;
# unexpected non-file/non-directory nodes fail the deploy.
sanitize_durable_paths() {
PG_MAJOR="$PG_MAJOR" python3 - <<'PY'
import os
from pathlib import Path
import shutil
import stat


def ensure_directory(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if stat.S_ISLNK(mode):
        os.unlink(path)
        path.mkdir(parents=True, exist_ok=True)
        return
    if not stat.S_ISDIR(mode):
        raise SystemExit(f"refusing to reuse non-directory managed path: {path}")


def ensure_regular_file_slot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        os.unlink(path)
        return
    if not stat.S_ISREG(mode):
        raise SystemExit(f"refusing to reuse non-regular managed path: {path}")


def recreate_directory(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        return
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    path.mkdir(parents=True, exist_ok=True)


admin_mount = Path("/mnt/kern-admin")
admin_state = admin_mount / "admin-state"
proxy_state = admin_mount / "proxy-state"
tools_state = admin_mount / "tools-state"
agent_home = Path("/mnt/kern-agent/agent-home")
pgdata = admin_mount / "postgres" / os.environ["PG_MAJOR"] / "main"
for directory in (
    # admin-state holds only the deploy-plane version.json; runtime admin
    # state lives in Postgres. Every component of the data directory path is
    # sanitized: the tree is owned by the postgres service user on a preserved
    # volume, so a compromised previous database process could otherwise plant
    # a symlink that a later root write follows.
    admin_state,
    admin_mount / "admin-home",
    admin_mount / "postgres",
    pgdata.parent,
    pgdata,
    proxy_state,
    tools_state,
    tools_state / "whatsapp",
    agent_home,
    agent_home / ".tmp",
    agent_home / ".codex",
    agent_home / ".claude",
    agent_home / ".hermes",
):
    ensure_directory(directory)
recreate_directory(proxy_state / "generated-certs")
recreate_directory(tools_state / "assets")

for path in (
    admin_state / "version.json",
    # Root rewrites these two managed files inside the postgres-owned data
    # directory on every deploy; the slots must not be symlinks.
    pgdata / "postgresql.conf",
    pgdata / "pg_hba.conf",
    proxy_state / "network_proxy_ca.key",
    proxy_state / "network_proxy_ca.crt",
    agent_home / "AGENTS.md",
    agent_home / "CLAUDE.md",
    agent_home / ".codex" / "config.toml",
    agent_home / ".claude" / "settings.json",
    agent_home / ".hermes" / "config.yaml",
    agent_home / ".hermes" / ".env",
):
    ensure_regular_file_slot(path)
PY
}

# Enforce the deploy operation against the authoritative admin disk version.
# The EC2 tag is only a discovery hint; this check runs after the durable admin
# volume is mounted, before preserved state is modified.
enforce_version_gate() {
python3 - <<'PY'
import json
import pathlib
import re
import sys

VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def parse_version(value: str) -> tuple[int, int, int]:
    if not VERSION_RE.fullmatch(value):
        fail(f"invalid Kern version {value!r}; expected MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in value.split("."))


def compare_versions(left: str, right: str) -> int:
    left_tuple = parse_version(left)
    right_tuple = parse_version(right)
    return (left_tuple > right_tuple) - (left_tuple < right_tuple)


payload = json.loads(pathlib.Path("/tmp/kern_payload.json").read_text())
operation = payload["operation"]
mode = operation["mode"]
target_version = operation["target_version"]
allow_upgrade = bool(operation.get("allow_upgrade"))
parse_version(target_version)

admin_state = pathlib.Path("/mnt/kern-admin/admin-state")
version_path = admin_state / "version.json"
config_path = admin_state / "config.json"
state_version = None
if version_path.exists():
    try:
        version_payload = json.loads(version_path.read_text())
    except json.JSONDecodeError as exc:
        fail(f"could not parse admin state version file {version_path}: {exc}")
    if not isinstance(version_payload, dict) or not isinstance(version_payload.get("version"), str):
        fail(f"admin state version file {version_path} must contain a string version field")
    state_version = version_payload["version"]
    parse_version(state_version)

if mode == "deploy":
    if state_version is not None or config_path.exists():
        fail("deploy requires empty admin state; use upgrade or recover for preserved state")
elif state_version is None:
    fail(f"{mode} requires existing admin state version file {version_path}")

if mode == "upgrade":
    comparison = compare_versions(state_version, target_version)
    if comparison >= 0:
        fail(
            f"upgrade requires admin state version lower than local VERSION; "
            f"state={state_version}, local={target_version}"
        )
elif mode == "recover":
    comparison = compare_versions(state_version, target_version)
    if allow_upgrade:
        if comparison > 0:
            fail(
                f"recover --allow-upgrade cannot move state backward; "
                f"state={state_version}, local={target_version}"
            )
    elif comparison != 0:
        fail(
            f"recover requires admin state version to match local VERSION; "
            f"state={state_version}, local={target_version}. Use recover --allow-upgrade to advance older state."
        )
elif mode == "reconfigure":
    comparison = compare_versions(state_version, target_version)
    if comparison != 0:
        fail(
            f"{mode} requires admin state version to match local VERSION; "
            f"state={state_version}, local={target_version}. Run upgrade first."
        )
elif mode != "deploy":
    fail(f"unknown deploy operation mode {mode!r}")

print(f"version check passed: mode={mode}, state={state_version or 'new'}, local={target_version}")
PY
}

# Install the Python runtime package copied by deploy. Runtime code is root
# owned but readable by service users.
install_runtime_code() {
mkdir -p /opt/kern-host
tar -xzf /tmp/kern-host-code.tar.gz -C /opt/kern-host
# The script runs with umask 077; the runtime code must stay root-owned but
# readable by the service users that import it.
chown -R root:root /opt/kern-host
chmod -R a+rX /opt/kern-host
chmod 644 /opt/kern-host/VERSION
}

# Fail the initial regional-mirror update quickly: it is safe to restart and
# its only purpose is to populate package indexes. Package installs keep the
# longer limits because terminating dpkg or a legitimate large download after
# one minute would be unsafe. The archive fallback also keeps the longer limits
# so a slow but working global mirror does not make the deployment brittle.
APT_ARCHIVE_FALLBACK_ACTIVE=false
APT_COMMAND_TIMEOUT=300s
APT_ACQUIRE_RETRIES=2
APT_ACQUIRE_TIMEOUT=20
APT_REGIONAL_UPDATE_COMMAND_TIMEOUT=60s
APT_REGIONAL_UPDATE_ACQUIRE_RETRIES=0
APT_REGIONAL_UPDATE_ACQUIRE_TIMEOUT=10

apt_get_once() {
  local command_timeout="$1" acquire_retries="$2" acquire_timeout="$3"
  shift 3
  timeout --signal=TERM --kill-after=30s "$command_timeout" \
    apt-get -q \
      -o DPkg::Lock::Timeout=300 \
      -o Acquire::Retries="$acquire_retries" \
      -o Acquire::http::Timeout="$acquire_timeout" \
      -o Acquire::https::Timeout="$acquire_timeout" \
      -o Acquire::Languages=none \
      -o APT::Update::Error-Mode=any \
      "$@"
}

has_ec2_ubuntu_archive_source() {
  local source_file
  local -a source_files
  shopt -s nullglob
  source_files=(
    /etc/apt/sources.list
    /etc/apt/sources.list.d/*.list
    /etc/apt/sources.list.d/*.sources
  )
  shopt -u nullglob
  for source_file in "${source_files[@]}"; do
    if grep -Eq '^[[:space:]]*(deb|URIs:).*https?://[^/]*\.ec2\.archive\.ubuntu\.com/ubuntu' "$source_file"; then
      return 0
    fi
  done
  return 1
}

switch_to_ubuntu_archive_fallback() {
  local changed=false source_file
  local -a source_files
  shopt -s nullglob
  source_files=(
    /etc/apt/sources.list
    /etc/apt/sources.list.d/*.list
    /etc/apt/sources.list.d/*.sources
  )
  shopt -u nullglob
  for source_file in "${source_files[@]}"; do
    if grep -Eq '^[[:space:]]*(deb|URIs:).*https?://[^/]*\.ec2\.archive\.ubuntu\.com/ubuntu' "$source_file"; then
      sed -i -E \
        's#https?://[^/]*\.ec2\.archive\.ubuntu\.com/ubuntu#http://archive.ubuntu.com/ubuntu#g' \
        "$source_file"
      changed=true
    fi
  done
  [[ "$changed" == true ]]
}

apt_get() {
  local command_timeout="$APT_COMMAND_TIMEOUT"
  local acquire_retries="$APT_ACQUIRE_RETRIES"
  local acquire_timeout="$APT_ACQUIRE_TIMEOUT"
  if [[ "$APT_ARCHIVE_FALLBACK_ACTIVE" == false && "${1:-}" == "update" ]] \
    && has_ec2_ubuntu_archive_source; then
    command_timeout="$APT_REGIONAL_UPDATE_COMMAND_TIMEOUT"
    acquire_retries="$APT_REGIONAL_UPDATE_ACQUIRE_RETRIES"
    acquire_timeout="$APT_REGIONAL_UPDATE_ACQUIRE_TIMEOUT"
  fi

  if apt_get_once "$command_timeout" "$acquire_retries" "$acquire_timeout" "$@"; then
    return 0
  fi
  if [[ "$APT_ARCHIVE_FALLBACK_ACTIVE" == true ]] || ! switch_to_ubuntu_archive_fallback; then
    return 1
  fi

  APT_ARCHIVE_FALLBACK_ACTIVE=true
  echo "apt-get failed against the regional EC2 Ubuntu mirror; retrying via archive.ubuntu.com" >&2
  if [[ "${1:-}" != "update" ]]; then
    apt_get_once "$APT_COMMAND_TIMEOUT" "$APT_ACQUIRE_RETRIES" "$APT_ACQUIRE_TIMEOUT" update
  fi
  apt_get_once "$APT_COMMAND_TIMEOUT" "$APT_ACQUIRE_RETRIES" "$APT_ACQUIRE_TIMEOUT" "$@"
}

# Install pgvector from the two architecture-specific packages committed with
# this pinned Kern revision. Keeping the small runtime packages in-tree removes
# the rolling PGDG repository and signing-key export from the fresh-deploy
# critical path while preserving exact, reproducible bytes. Building the same
# extension from source pulled roughly 165 MiB of compiler/LLVM archives and
# installed 812 MiB temporarily on every deployment.
install_pgvector_package() {
local pg_arch pgvector_deb pgvector_digest pgvector_extract pgvector_share
local -a pgvector_sql
pg_arch="$(dpkg --print-architecture)"
case "$pg_arch" in
  amd64) pgvector_digest="$PGVECTOR_DEB_SHA256_AMD64" ;;
  arm64) pgvector_digest="$PGVECTOR_DEB_SHA256_ARM64" ;;
  *) echo "unsupported pgvector architecture: $pg_arch" >&2; return 1 ;;
esac
pgvector_deb="${VENDOR_SOURCE_DIR}/pgvector/postgresql-${PG_MAJOR}-pgvector_${PGVECTOR_DEB_VERSION}_${pg_arch}.deb"
test -f "$pgvector_deb"
echo "${pgvector_digest}  ${pgvector_deb}" | sha256sum --check --status
test "$(dpkg-deb --field "$pgvector_deb" Package)" = "postgresql-${PG_MAJOR}-pgvector"
test "$(dpkg-deb --field "$pgvector_deb" Version)" = "$PGVECTOR_DEB_VERSION"
test "$(dpkg-deb --field "$pgvector_deb" Architecture)" = "$pg_arch"

# PGDG packages LLVM bitcode and consequently declares a Breaks relationship
# with Ubuntu's postgresql-14-jit-llvm provider. Kern does not need extension
# bitcode. Extract only pgvector's runtime library and extension SQL/control
# files, leaving Ubuntu's PostgreSQL package and dpkg state untouched.
pgvector_extract="$(mktemp -d /tmp/kern-pgvector-extract.XXXXXX)"
dpkg-deb --extract "$pgvector_deb" "$pgvector_extract"
pgvector_share="$pgvector_extract/usr/share/postgresql/${PG_MAJOR}/extension"
test -f "$pgvector_extract/usr/lib/postgresql/${PG_MAJOR}/lib/vector.so"
test -f "$pgvector_share/vector.control"
shopt -s nullglob
pgvector_sql=("$pgvector_share"/vector--*.sql)
shopt -u nullglob
test "${#pgvector_sql[@]}" -gt 0
install -o root -g root -m 0644 \
  "$pgvector_extract/usr/lib/postgresql/${PG_MAJOR}/lib/vector.so" \
  "/usr/lib/postgresql/${PG_MAJOR}/lib/vector.so"
install -o root -g root -m 0644 "$pgvector_share/vector.control" \
  "/usr/share/postgresql/${PG_MAJOR}/extension/vector.control"
install -o root -g root -m 0644 "${pgvector_sql[@]}" \
  "/usr/share/postgresql/${PG_MAJOR}/extension/"
rm -rf "$pgvector_extract"
}

# Base OS packages.
install_system_packages() {
echo "== installing system packages =="
# First boot races the AMI's apt-daily/apt-daily-upgrade timers: both are
# persistent, so on a fresh instance they fire within seconds of launch and
# hold the apt/dpkg locks while downloading the pending security batch, and
# the installs below wait on the lock (2026-07-27: two smokes spent 13 minutes
# in this normally 40-second phase). Stop them for the duration of this
# function; they are restarted below and patch the host in the background off
# the deploy critical path.
systemctl stop apt-daily.timer apt-daily-upgrade.timer
systemctl stop apt-daily.service apt-daily-upgrade.service

# Node.js (and npm) come from the official tarball below, not apt: the Ubuntu
# npm package pulls in hundreds of node-* dependencies.
apt_get update
apt_get install -y ca-certificates curl gh git jq nftables openssl python3 python3-venv sudo unattended-upgrades xz-utils

# PostgreSQL for admin state. postgresql-common is installed first so its
# default-cluster creation can be disabled: the data directory must live on
# the durable admin volume (set up below), not on this replaceable root volume.
apt_get install -y postgresql-common
sed -i 's/^#\?create_main_cluster.*/create_main_cluster = false/' /etc/postgresql-common/createcluster.conf
apt_get install -y "postgresql-${PG_MAJOR}"
# pgvector keeps semantic-search vectors in the existing durable Postgres
# database. Install the pinned, repository-signed binary without retaining a
# third-party apt source on the deployed host.
install_pgvector_package
# The packaged umbrella unit only manages clusters registered with the Debian
# tooling; the Kern cluster runs under its own unit below.
systemctl disable --now postgresql.service >/dev/null 2>&1 || true

# Security updates are deliberately not applied inline: the pending batch is
# unbounded (it grows with the age of the current Canonical AMI) and would put
# archive download time on the deploy critical path. The restarted timers run
# unattended-upgrades in the background shortly after boot instead, so a fresh
# host is patched within the hour without delaying the deploy.
systemctl start apt-daily.timer apt-daily-upgrade.timer
}

# Idempotent legacy migration for releases that provisioned one PostgreSQL
# role and schema per generic app. These operations require the postgres
# superuser, so they intentionally live at the bootstrap identity boundary
# instead of the kern-admin schema migration stream. Retained Chat/Web Apps
# objects move directly to the host migration owner; removed app identities
# and schemas are retired. Fresh hosts simply find no legacy roles or schemas.
migrate_legacy_app_identities() {
  local legacy_role
  for legacy_role in kern-app-0 kern-app-6; do
    if runuser -u postgres -- psql -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname = '${legacy_role}'" | grep -q 1; then
      runuser -u postgres -- psql -d kern_admin -v ON_ERROR_STOP=1 --quiet \
        -c "REASSIGN OWNED BY \"${legacy_role}\" TO \"kern-admin\";" \
        -c "DROP OWNED BY \"${legacy_role}\";"
      runuser -u postgres -- dropuser "${legacy_role}"
    fi
  done
  runuser -u postgres -- psql -d kern_admin -v ON_ERROR_STOP=1 --quiet \
    -c "DROP SCHEMA IF EXISTS app_mission_pursuit CASCADE;" \
    -c "DROP SCHEMA IF EXISTS app_alpha_seeker CASCADE;" \
    -c "DROP SCHEMA IF EXISTS app_social_marketer CASCADE;" \
    -c "DROP SCHEMA IF EXISTS app_virality_machine CASCADE;" \
    -c "DROP SCHEMA IF EXISTS app_software_builder CASCADE;"
  for legacy_role in kern-app-1 kern-app-2 kern-app-3 kern-app-4 kern-app-5; do
    if runuser -u postgres -- psql -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname = '${legacy_role}'" | grep -q 1; then
      runuser -u postgres -- psql -d kern_admin -v ON_ERROR_STOP=1 --quiet \
        -c "DROP OWNED BY \"${legacy_role}\";"
      runuser -u postgres -- dropuser "${legacy_role}"
    fi
  done
}

setup_postgres() {
echo "== setting up admin-state PostgreSQL =="
PG_BIN="/usr/lib/postgresql/${PG_MAJOR}/bin"
install -d -o postgres -g postgres -m 700 "$(dirname "$PGDATA_DIR")" "$PGDATA_DIR"
chown root:root /mnt/kern-admin/postgres
chmod 711 /mnt/kern-admin/postgres
if [ ! -f "$PGDATA_DIR/PG_VERSION" ]; then
  runuser -u postgres -- "$PG_BIN/initdb" -D "$PGDATA_DIR" --auth-local=peer --auth-host=reject -E UTF8
fi

# Managed database config, rewritten on every deploy like the rest of the
# root-of-trust config. Unix-socket only: there is no TCP listener at all, and
# peer auth maps OS users to database roles, so access control is the host's
# user model. kern-admin (owner of the admin database), the scoped
# kern-proxy, kern-tools, and kern-agent-network roles, and the postgres superuser
# (operators, via sudo) can connect; every other user —
# including the agent user — matches only the final reject rule.
cat > "$PGDATA_DIR/postgresql.conf" <<PGCONF
# Managed by Kern bootstrap; rewritten on every deploy.
listen_addresses = ''
unix_socket_directories = '/var/run/postgresql'
# Each service process bounds its own active sessions client-side
# (db.MAX_ACTIVE_CONNECTIONS = 14). Keep a fixed ceiling with ample room for
# the admin, proxy, tools, network-introspection, and Workspace runtime pools plus
# operator psql, the superuser reserve, deployment work, and future fixed host
# services. Bursts beyond a process's cap queue client-side instead of
# immediately failing at the server.
max_connections = 300
log_destination = 'stderr'
PGCONF
cat > "$PGDATA_DIR/pg_hba.conf" <<'PGHBA'
# Managed by Kern bootstrap; rewritten on every deploy.
# Unix-socket connections only; identity is the OS user (peer auth). Schema
# migrations give proxy, tools, network-introspection, and Workspace roles only their
# required tables or schemas. The agent user has no role and no rule that
# admits it.
local  kern_admin  kern-admin  peer
local  kern_admin  kern-proxy  peer
local  kern_admin  kern-tools  peer
local  kern_admin  kern-agent-network  peer
local  kern_admin  kern-workspace  peer
local  all               postgres          peer
local  all               all               reject
PGHBA
chown postgres:postgres "$PGDATA_DIR/postgresql.conf" "$PGDATA_DIR/pg_hba.conf"
chmod 600 "$PGDATA_DIR/postgresql.conf" "$PGDATA_DIR/pg_hba.conf"

cat > /etc/systemd/system/kern-postgres.service <<UNIT
[Unit]
Description=Kern Admin State PostgreSQL
After=local-fs.target
# Admin state is unreachable without it, and it is local-only, so a crash
# loop must keep retrying rather than hit the default start-limit.
StartLimitIntervalSec=0

[Service]
Type=notify
User=postgres
ExecStart=$PG_BIN/postgres -D $PGDATA_DIR
ExecStopPost=/usr/bin/env PYTHONPATH=/opt/kern-host /usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-postgres
Restart=always
RestartSec=3
TimeoutStartSec=300
RuntimeDirectory=postgresql
RuntimeDirectoryPreserve=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now kern-postgres.service
for attempt in $(seq 1 60); do
  if runuser -u postgres -- "$PG_BIN/pg_isready" -q; then
    break
  fi
  if [ "$attempt" = 60 ]; then
    echo "PostgreSQL did not become ready" >&2
    exit 1
  fi
  sleep 1
done

# Role and database, created once; both survive redeploys inside the data
# directory. The role name matches the service's Unix user so peer auth works.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 --quiet <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-admin') THEN
    CREATE ROLE "kern-admin" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-proxy') THEN
    CREATE ROLE "kern-proxy" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-tools') THEN
    CREATE ROLE "kern-tools" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-agent-network') THEN
    CREATE ROLE "kern-agent-network" LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kern-workspace') THEN
    CREATE ROLE "kern-workspace" LOGIN;
  END IF;
END
$$;
SQL
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'kern_admin'" | grep -q 1; then
  runuser -u postgres -- createdb --owner=kern-admin kern_admin
fi
# pgvector is an untrusted PostgreSQL extension and therefore must be installed
# by the database superuser. Migrations run afterward as the non-superuser
# kern-admin owner and create only Kern's derived-vector table and indexes.
runuser -u postgres -- psql -d kern_admin -v ON_ERROR_STOP=1 --quiet \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
migrate_legacy_app_identities
runuser -u postgres -- psql -d kern_admin -v ON_ERROR_STOP=1 --quiet \
  -c "REVOKE ALL ON DATABASE kern_admin FROM PUBLIC;" \
  -c "REVOKE CREATE ON SCHEMA public FROM PUBLIC;" \
  -c "GRANT CREATE ON SCHEMA public TO \"kern-admin\";" \
  -c "GRANT CONNECT ON DATABASE kern_admin TO \"kern-proxy\";" \
  -c "GRANT CONNECT ON DATABASE kern_admin TO \"kern-tools\";" \
  -c "GRANT CONNECT ON DATABASE kern_admin TO \"kern-agent-network\";" \
  -c 'GRANT CONNECT ON DATABASE kern_admin TO "kern-workspace";'
# The PUBLIC revoke also stripped the proxy, tools, and agent-network roles'
# inherited CONNECT, so it is granted back explicitly; without it the proxy
# cannot log network decisions (and, being fail-closed, would fail every agent
# request), and the tools service cannot reach any tool state. PostgreSQL 14 ships the public schema
# creatable by every connecting role, so CREATE is revoked there too and
# granted back to exactly the schema-owning admin role: a compromised proxy,
# tools, or agent-network service can use only its granted tables, not mint new
# objects. The owning kern-admin role keeps its database privileges
# implicitly.
}

adopt_workspace_migration_history() {
  # One-time, idempotent ledger migration. Releases before the fixed workspace
  # service recorded Chat and Web Apps independently. Mirror every applied
  # old version into its renumbered host migration before the unified runner
  # decides what remains pending. Fresh databases simply insert no rows; once
  # migration 0026 removes the old ledger, later bootstraps are a no-op.
  runuser -u kern-admin -- psql -d kern_admin -v ON_ERROR_STOP=1 --quiet <<'SQL'
DO $$
BEGIN
IF to_regclass('public.workspace_migrations') IS NULL THEN
  RETURN;
END IF;
-- Dynamic SQL defers resolving the legacy table until after the existence
-- guard, so this block remains valid after migration 0026 drops that table.
EXECUTE $adopt$
INSERT INTO public.schema_migrations (version, name, applied_at)
SELECT mapping.new_version, mapping.new_name, old.applied_at
FROM public.workspace_migrations AS old
JOIN (VALUES
  ('chat', 1, 15, 'workspace_chat_baseline'),
  ('chat', 2, 16, 'workspace_chat_thread_names'),
  ('chat', 3, 17, 'workspace_chat_drop_thread_tasks'),
  ('web_apps', 1, 18, 'workspace_web_app_state'),
  ('web_apps', 2, 19, 'workspace_web_builder_thread_reset'),
  ('web_apps', 3, 20, 'workspace_multiple_web_apps'),
  ('web_apps', 4, 21, 'workspace_web_app_platform'),
  ('web_apps', 5, 22, 'workspace_web_remove_archiving'),
  ('web_apps', 6, 23, 'workspace_web_memory_revision'),
  ('web_apps', 7, 24, 'workspace_web_restore_archiving')
) AS mapping(workspace_kind, old_version, new_version, new_name)
  ON old.workspace_kind = mapping.workspace_kind AND old.version = mapping.old_version
ON CONFLICT (version) DO NOTHING
$adopt$;
END
$$;
SQL
}

# Apply schema migrations, then compute and store the effective host config.
# Both run as kern-admin: migrations are owned by the same role the
# service uses, so the service never needs DDL rights it does not already
# have, and on upgrade/recover write_config carries the stored password and
# operator connections over from the existing config table. write_config echoes
# the effective config, which is staged root-only for the later bootstrap steps
# (SSH keys, cloudflared) that need it without database access.
migrate_admin_state_and_write_config() {
echo "== migrating admin state schema =="
# The kern-tools role's table grants live in the baseline schema migration
# (0001_baseline.sql), the same pattern as the kern-proxy grants;
# bootstrap only provisions the role, its pg_hba line, and database CONNECT
# above, before migrations run.
runuser -u kern-admin -- env PYTHONPATH=/opt/kern-host python3 -m host.runtime.deploy.migrate up --to 13
adopt_workspace_migration_history
runuser -u kern-admin -- env PYTHONPATH=/opt/kern-host python3 -m host.runtime.deploy.migrate up
python3 - <<'PY' | runuser -u kern-admin -- env PYTHONPATH=/opt/kern-host python3 -m host.runtime.deploy.write_config > /tmp/kern_effective_config.json
import json, pathlib
payload = json.loads(pathlib.Path('/tmp/kern_payload.json').read_text())
print(json.dumps({
    'mode': payload['operation']['mode'],
    'runtime_config': payload['runtime_config'],
    'reset_admin_passkeys': bool(payload['operation'].get('reset_admin_passkeys', False)),
}))
PY
chmod 600 /tmp/kern_effective_config.json
}

# Apply or remove persistent SSH operator access from the effective config.
# EC2 user data installs only the generated deploy key long enough for
# bootstrap to start.
configure_operator_ssh() {
python3 - <<'PY'
import json, pathlib

config = json.loads(pathlib.Path('/tmp/kern_effective_config.json').read_text())
ssh_keys = [
    connection['ssh_public_key']
    for connection in config['operator_connections']
    if connection.get('mode') == 'ssh'
]
ssh_dir = pathlib.Path('/home/kern-operator/.ssh')
ssh_dir.mkdir(parents=True, exist_ok=True)
authorized_keys = ssh_dir / 'authorized_keys'
if ssh_keys:
    authorized_keys.write_text('\n'.join(key.rstrip() for key in ssh_keys) + '\n')
else:
    authorized_keys.unlink(missing_ok=True)
PY
chown -R kern-operator:kern-operator /home/kern-operator/.ssh
chmod 700 /home/kern-operator/.ssh
if [ -f /home/kern-operator/.ssh/authorized_keys ]; then
  chmod 600 /home/kern-operator/.ssh/authorized_keys
fi
}

# Runtime CLIs used by the unprivileged agent user.
install_agent_clis() {
echo "== installing Node.js ${NODE_VERSION} =="
arch="$(dpkg --print-architecture)"
case "$arch" in
  amd64) node_arch=x64 ;;
  arm64) node_arch=arm64 ;;
  *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;;
esac
curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"
tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner
rm -f /tmp/node.tar.xz

echo "== installing host Node dependencies =="
install -d -m 755 -o root -g root /usr/local/lib/kern-node
install -m 0644 -o root -g root \
  /opt/kern-host/host/npm/package.json \
  /opt/kern-host/host/npm/package-lock.json \
  /usr/local/lib/kern-node/
npm ci --prefix /usr/local/lib/kern-node --omit=dev --no-fund --no-audit --loglevel=error
chown -R root:root /usr/local/lib/kern-node
# bootstrap's umask is 077, but kern-tools must be able to traverse and read
# these root-owned dependencies without being able to modify them.
chmod -R u=rwX,go=rX /usr/local/lib/kern-node

echo "== installing Codex CLI =="
npm install -g --no-fund --no-audit --loglevel=error "@openai/codex@${CODEX_CLI_VERSION}"
echo "== installing Claude Code CLI =="
npm install -g --no-fund --no-audit --loglevel=error "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
echo "== installing Grok CLI =="
# The npm package is a Node trampoline plus a brotli-compressed per-platform
# binary. Left to itself the trampoline decompresses that binary into
# $GROK_HOME/bin on first run and execs it from there — inside the agent's own
# home, where the agent could replace the very binary the launcher runs. It
# would also rewrite $GROK_HOME/config.toml on install.
#
# So the payload is decompressed here instead, to a root-owned path, and that
# is what run-grok execs. It matches how the Codex and Claude Code CLIs sit
# (root-owned under /usr/local, agent-readable, agent-unwritable), and it is
# what makes the version pin and the launcher's flags hold at runtime rather
# than depending on the agent leaving its own home alone. The trampoline's
# /usr/local/bin/grok symlink is replaced, so there is exactly one grok on the
# box and no PATH lookup can reach an agent-writable one.
npm install -g --no-fund --no-audit --loglevel=error "@xai-official/grok@${GROK_CLI_VERSION}"
grok_payload="/usr/local/lib/node_modules/@xai-official/grok/node_modules/@xai-official/grok-linux-${node_arch}/bin/grok.br"
if [ ! -f "$grok_payload" ]; then
  echo "missing Grok binary payload at ${grok_payload}" >&2
  exit 1
fi
rm -f /usr/local/bin/grok
node -e '
const fs = require("fs"), zlib = require("zlib");
fs.writeFileSync(process.argv[2], zlib.brotliDecompressSync(fs.readFileSync(process.argv[1])));
' "$grok_payload" /usr/local/bin/grok
chown root:root /usr/local/bin/grok
chmod 0755 /usr/local/bin/grok
echo "== installing Hermes agent =="
case "$arch" in
  amd64) uv_arch=x86_64-unknown-linux-gnu ;;
  arm64) uv_arch=aarch64-unknown-linux-gnu ;;
  *) echo "unsupported uv architecture: ${arch}" >&2; exit 1 ;;
esac
curl -fsSLo /tmp/uv.tar.gz "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${uv_arch}.tar.gz"
tar -xzf /tmp/uv.tar.gz -C /tmp
install -m 0755 "/tmp/uv-${uv_arch}/uv" /usr/local/bin/uv
rm -rf /tmp/uv.tar.gz "/tmp/uv-${uv_arch}"
export UV_PYTHON_INSTALL_DIR=/usr/local/lib/hermes-python
uv python install "${HERMES_PYTHON_VERSION}"
uv venv --python "${HERMES_PYTHON_VERSION}" /usr/local/lib/hermes-venv
# The bedrock extra brings the boto3 Converse transport; the mcp extra brings
# the MCP client SDK, which the managed ~/.hermes/config.yaml needs to spawn
# the bundled-tools MCP shim (mcp_servers.kern).
uv pip install --python /usr/local/lib/hermes-venv/bin/python \
  "hermes-agent[bedrock,mcp]==${HERMES_AGENT_VERSION}"
chmod -R a+rX /usr/local/lib/hermes-python /usr/local/lib/hermes-venv

# The local conversation-and-memory search encoder has its own dependency environment;
# the host runtime remains standard-library-only. Install the pinned compact
# model while bootstrap still has network access. The runtime service is
# PrivateNetwork=yes and reads the installed files directly.
#
# The model is fetched from Kern's own public release, not from HuggingFace.
# fastembed resolves its download revision with model_info(repo).sha at call
# time and exposes no revision argument, so letting it download would take
# whatever the upstream repository's main branch pointed at during this deploy,
# unpinned and unchecksummed. Serving the artifact from the release Kern is
# already fetched from keeps the deploy on one trusted, pinned channel and
# removes huggingface.co from the critical path.
uv venv --python /usr/bin/python3 /usr/local/lib/kern-embedding-venv
uv pip install --python /usr/local/lib/kern-embedding-venv/bin/python \
  "fastembed==${FASTEMBED_VERSION}"
install -d -o root -g root -m 0755 /usr/local/share/kern-embedding-models
install -d -o root -g root -m 0755 "$EMBEDDING_MODEL_DIR"
embedding_model_base="https://github.com/@GITHUB_REPOSITORY@/releases/download/${EMBEDDING_MODEL_TAG}"
while read -r _digest embedding_model_file; do
  curl -fsSL --retry 5 --retry-all-errors --retry-delay 2 \
    -o "${EMBEDDING_MODEL_DIR}/${embedding_model_file}" \
    "${embedding_model_base}/${embedding_model_file}"
done <<< "$EMBEDDING_MODEL_SHA256"
# Verify against the digests pinned in this script, never against a checksum
# file served alongside the assets: anything able to replace an asset could
# replace that file too. A moved or re-cut release tag therefore fails loudly
# here instead of installing a different model.
(
  cd "$EMBEDDING_MODEL_DIR"
  printf '%s\n' "$EMBEDDING_MODEL_SHA256" | sha256sum --check --status
)
EMBEDDING_MODEL_DIR="$EMBEDDING_MODEL_DIR" \
  /usr/local/lib/kern-embedding-venv/bin/python - <<'PY'
import os

from fastembed import TextEmbedding

# specific_model_path short-circuits fastembed's cache and HuggingFace lookup
# entirely and loads this directory as-is, so the installed layout is a flat
# directory rather than a reconstructed hub cache.
model = TextEmbedding(
    model_name="BAAI/bge-small-en-v1.5",
    specific_model_path=os.environ["EMBEDDING_MODEL_DIR"],
    threads=1,
)
vectors = list(model.query_embed(["Kern local embedding readiness check"]))
if len(vectors) != 1 or len(vectors[0]) != 384:
    raise SystemExit("local embedding model returned an unexpected shape")
PY
chmod -R a+rX /usr/local/lib/kern-embedding-venv /usr/local/share/kern-embedding-models
# npm inherits the script's umask 077, which would leave the CLI root-only;
# the agent user must be able to run it.
chmod -R a+rX /usr/local/lib/node_modules
}

configure_cloudflared() {
cloudflare_connection_count="$(python3 - <<'PY'
import json, pathlib
config = json.loads(pathlib.Path('/tmp/kern_effective_config.json').read_text())
print(sum(1 for connection in config['operator_connections'] if connection.get('mode') == 'cloudflare_tunnel'))
PY
)"
if [ "$cloudflare_connection_count" -gt 0 ]; then
  echo "== installing cloudflared ${CLOUDFLARED_VERSION} =="
  case "$arch" in
    amd64) cloudflared_arch=amd64 ;;
    arm64) cloudflared_arch=arm64 ;;
    *) echo "unsupported cloudflared architecture: ${arch}" >&2; exit 1 ;;
  esac
  curl -fsSLo /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${cloudflared_arch}"
  chmod 755 /usr/local/bin/cloudflared
  cloudflared_version="$(/usr/local/bin/cloudflared --version)"
  case "$cloudflared_version" in
    *"${CLOUDFLARED_VERSION}"*) ;;
    *) echo "unexpected cloudflared version: ${cloudflared_version}" >&2; exit 1 ;;
  esac
  install -m 0750 -o root -g cloudflared -d /etc/kern
  python3 - <<'PY'
import json, pathlib
config = json.loads(pathlib.Path('/tmp/kern_effective_config.json').read_text())
connections = [
    connection
    for connection in config['operator_connections']
    if connection.get('mode') == 'cloudflare_tunnel'
]
if len(connections) != 1:
    raise SystemExit(f'expected exactly one cloudflare_tunnel connection, found {len(connections)}')
connection = connections[0]
pathlib.Path('/etc/kern/cloudflared.token').write_text(connection['tunnel_token'].strip() + '\n')
pathlib.Path('/etc/kern/cloudflare_hostname').write_text(connection['hostname'] + '\n')
PY
  chown root:cloudflared /etc/kern/cloudflared.token
  chown root:root /etc/kern/cloudflare_hostname
  chmod 640 /etc/kern/cloudflared.token
  chmod 644 /etc/kern/cloudflare_hostname
else
  rm -f /etc/systemd/system/kern-cloudflared.service /etc/kern/cloudflared.token /etc/kern/cloudflare_hostname
fi
}

# Managed Codex policy: restrict the agent to cached web search and disable
# Codex-hosted app/plugin/browse surfaces so the agent does not even attempt a
# tool the proxy would deny. `cached` is the only allowed web-search mode, which
# structurally excludes both `live` and `indexed` (server-approved external URL
# fetch) and keeps `open_page`/`find_in_page` reading OpenAI's index rather than
# fetching live — so no separate knob is needed for those. The network proxy is
# the ultimate layer: it denies any non-cached web/browse tool on OpenAI domains
# regardless of what the client requests.
write_codex_policy() {
mkdir -p /etc/codex
chmod 755 /etc/codex
# Codex 0.144 enables zstd request compression by default. Pin it off with the
# other enforced feature requirements so the network-policy proxy can inspect
# uncompressed request JSON fail-closed.
cat > /etc/codex/requirements.toml <<'EOF'
allowed_web_search_modes = ["cached"]

[features]
enable_request_compression = false
apps = false
plugins = false
tool_search = false
tool_suggest = false
computer_use = false
remote_plugin = false
plugin_sharing = false
EOF
chmod 644 /etc/codex/requirements.toml

# Managed Codex config layer: the bundled tools surface. Codex spawns the
# MCP shim as kern-agent; the shim forwards to the tools service's
# socket, which authenticates the caller by kernel peer credentials.
# managed_config.toml is the documented root-owned system layer Codex loads
# alongside the agent-home config.
cat > /etc/codex/managed_config.toml <<'EOF'
[mcp_servers.kern]
command = "/usr/bin/python3"
args = ["-m", "host.runtime.agent_shim.mcp_shim"]
env = { PYTHONPATH = "/opt/kern-host" }
EOF
chmod 644 /etc/codex/managed_config.toml
}

# Grok layers config lowest-to-highest as: /etc/grok/managed_config.toml,
# $GROK_HOME/managed_config.toml, $GROK_HOME/config.toml,
# $GROK_HOME/requirements.toml, then /etc/grok/requirements.toml. The last is
# root-owned and wins outright, so it is where host policy goes: the agent owns
# its GROK_HOME and can rewrite every layer below it.
#
# This layer is defence in depth, not the enforcement. The network proxy
# authoritatively decides server-side tool use by inspecting every request
# body, whether or not the CLI honours the settings here. What this file owns
# is the matching client posture
# that never varies: no product telemetry, no trace/research uploads, no
# self-update, and no inheriting another runtime's MCP servers. The account-
# backed "Coding data, retention, and training" choice is not a config.toml
# setting; Grok exposes that separately through /privacy and /settings.
write_grok_policy() {
mkdir -p /etc/grok
chmod 755 /etc/grok
# compat.claude and compat.cursor matter specifically on this host: Grok scans
# ~/.claude.json, ~/.cursor/mcp.json and project .mcp.json for MCP servers by
# default, and the agent home has a .claude directory belonging to another
# runtime. Left on, Grok would inherit whatever MCP servers that runtime was
# configured with.
cat > /etc/grok/requirements.toml <<'EOF'
[features]
telemetry = false
auto_update = false
plugins = false
marketplace = false

[telemetry]
trace_upload = false

[compat.claude]
mcps = false

[compat.cursor]
mcps = false

[cli]
use_leader = false

# --disable-web-search removes Grok's client web_search and web_fetch tools.
# Grok 1.0.5 separately injects the hosted x_search capability when the model
# advertises backend search; keep that X-only capability on. The proxy still
# denies web search and every hosted tool outside its explicit xAI/X allowlist.
[model."grok-4.6"]
supports_backend_search = true

# The only MCP server Grok may inherit is Kern's bundled-tools shim. Keeping
# it in the root-owned highest-precedence layer prevents the agent-owned Grok,
# Claude and Cursor config files from replacing this host boundary.
[mcp_servers.kern]
command = "/usr/bin/python3"
args = ["-m", "host.runtime.agent_shim.mcp_shim"]
env = { PYTHONPATH = "/opt/kern-host" }
EOF
chmod 644 /etc/grok/requirements.toml
}

harden_base_os() {
# Reduce the root-daemon attack surface reachable by the agent. snapd ships in
# the base image but is unused here, and its socket is world-accessible; stop
# and mask it so the agent has no snapd API to reach for privilege escalation.
systemctl disable --now snapd.socket snapd.service >/dev/null 2>&1 || true
systemctl mask snapd.socket snapd.service >/dev/null 2>&1 || true

# Root-volume swap is replaceable on redeploy, unlike admin/agent data.
if [ ! -f /swapfile ]; then
  fallocate -l 6G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
}

# Persistent proxy CA. The proxy user owns the private key after ownership is
# fixed below; the public CA is installed into the system trust store.
setup_proxy_ca() {
if [ ! -f "$PROXY_STATE_DIR/network_proxy_ca.key" ] || [ ! -f "$PROXY_STATE_DIR/network_proxy_ca.crt" ]; then
  openssl req -x509 -newkey rsa:4096 -nodes \
    -keyout "$PROXY_STATE_DIR/network_proxy_ca.key" \
    -out "$PROXY_STATE_DIR/network_proxy_ca.crt" \
    -days 3650 -subj "/CN=Kern Network Proxy"
fi
cp "$PROXY_STATE_DIR/network_proxy_ca.crt" /usr/local/share/ca-certificates/kern-network-proxy.crt
chmod 644 /usr/local/share/ca-certificates/kern-network-proxy.crt
update-ca-certificates
}

# Narrow root-owned helper scripts. Admin may invoke these exact paths through
# sudo, and helpers demote to agent/proxy users for runtime work.
install_sudo_helpers() {
mkdir -p /usr/local/lib/kern-host "$PROXY_STATE_DIR/generated-certs"
chmod 755 /usr/local/lib/kern-host
HELPER_SOURCE_DIR=/opt/kern-host/host/bootstrap/helpers
AGENT_HOME_SOURCE_DIR=/opt/kern-host/host/bootstrap/agent-home
HELPER_NAMES=(
  run-codex-app-server
  read-codex-account-id
  run-claude-code
  read-claude-account
  run-grok
  read-grok-account
  run-hermes
  run-agent-script
  stop-agent-thread
  read-aws-account
  clear-agent-auth
  read-agent-file
  upload-agent-file
  reboot-host
  check-for-upgrade
  mint-github-app-token
  audit-github-repo
  approve-github-push
)
for helper_name in "${HELPER_NAMES[@]}"; do
  sed "s/@""PROXY_PORT@/${PROXY_PORT}/g" \
    "$HELPER_SOURCE_DIR/${helper_name}.sh" \
    > "/usr/local/lib/kern-host/${helper_name}"
done
install -m 0755 "$HELPER_SOURCE_DIR/hermes-stdin.py" /usr/local/lib/kern-host/hermes-stdin.py
chown root:root /usr/local/lib/kern-host/*
chmod 755 /usr/local/lib/kern-host/*

# GitHub credentials are injected by the network proxy (the agent never
# holds the token), so the only client wiring is the gh shim: gh refuses to
# run authenticated calls without GH_TOKEN, so the shim supplies a fixed
# placeholder that the proxy strips and replaces. /usr/local/bin precedes
# /usr/bin on PATH, so the shim shadows the packaged gh. git needs no wiring
# at all — its unauthenticated requests are authenticated in transit.
sed "s/@""PROXY_PORT@/${PROXY_PORT}/g" "$HELPER_SOURCE_DIR/gh-shim.sh" > /usr/local/bin/gh
chown root:root /usr/local/bin/gh
chmod 755 /usr/local/bin/gh
}

# Final durable-volume ownership, one declarative row per managed path:
# "path owner:group mode". Avoid recursive chown across preserved mutable
# trees; only directory roots and known managed files are adjusted.
# host.bootstrap.verify_deploy independently re-checks these facts after the
# services start.
DURABLE_PATH_OWNERSHIP="
/mnt/kern-admin root:root 711
/mnt/kern-agent root:root 711
/mnt/kern-admin/admin-state kern-admin:kern-admin 700
/mnt/kern-admin/admin-home kern-admin:kern-admin 700
/mnt/kern-agent/agent-home kern-agent:kern-agent 700
/mnt/kern-admin/proxy-state kern-proxy:kern-proxy 700
/mnt/kern-admin/proxy-state/generated-certs kern-proxy:kern-proxy 700
/mnt/kern-admin/proxy-state/network_proxy_ca.key kern-proxy:kern-proxy 600
/mnt/kern-admin/proxy-state/network_proxy_ca.crt kern-proxy:kern-proxy 644
/mnt/kern-admin/tools-state kern-tools:kern-tools 700
/mnt/kern-admin/tools-state/assets kern-tools:kern-tools 700
/mnt/kern-admin/tools-state/whatsapp kern-tools:kern-tools 700
"

apply_durable_ownership() {
  local row_path row_owner row_mode
  while read -r row_path row_owner row_mode; do
    [ -n "$row_path" ] || continue
    chown "$row_owner" "$row_path"
    chmod "$row_mode" "$row_path"
  done <<< "$DURABLE_PATH_OWNERSHIP"
  install -d -m 700 -o kern-agent -g kern-agent \
    "$AGENT_HOME_PATH/.tmp" \
    "$AGENT_HOME_PATH/.codex" \
    "$AGENT_HOME_PATH/.claude" \
    "$AGENT_HOME_PATH/.grok" \
    "$AGENT_HOME_PATH/.hermes"
  # Agent processes normally remove their own temporary trees. Cover abrupt
  # kills and host crashes as well: the standard daily tmpfiles timer removes
  # abandoned scratch after one day. Agent turns are bounded to minutes, so
  # the age leaves ample room for every live runtime.
  cat > /etc/tmpfiles.d/kern-agent.conf <<'TMPFILES'
d /mnt/kern-agent/agent-home/.tmp 0700 kern-agent kern-agent 1d -
TMPFILES
  systemd-tmpfiles --create --clean /etc/tmpfiles.d/kern-agent.conf
  # systemd-tmpfiles-clean.timer is a static dependency of timers.target. It
  # reads this rule on its normal schedule; Kern must not enable, start, or gate
  # bootstrap on the transient state of that system-owned timer.
  # No initial network policy is seeded: a missing policy row is the
  # fail-closed empty default (deny everything).
}

# Durable agent runtime config and instructions. The files live in this repo so
# harness expectations are reviewable, and bootstrap installs them root-owned,
# world-readable, and immutable so the agent can read but not alter them.
install_agent_home_files() {
for managed_agent_file in \
  "$AGENT_HOME_PATH/AGENTS.md" \
  "$AGENT_HOME_PATH/CLAUDE.md" \
  "$AGENT_HOME_PATH/.codex/config.toml" \
  "$AGENT_HOME_PATH/.claude/settings.json" \
  "$AGENT_HOME_PATH/.hermes/config.yaml" \
  "$AGENT_HOME_PATH/.hermes/.env"; do
  chattr -f -i "$managed_agent_file" || true
done
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/agents_claude.md" "$AGENT_HOME_PATH/AGENTS.md"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/agents_claude.md" "$AGENT_HOME_PATH/CLAUDE.md"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/.codex/config.toml" "$AGENT_HOME_PATH/.codex/config.toml"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/.claude/settings.json" "$AGENT_HOME_PATH/.claude/settings.json"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/.hermes/config.yaml" "$AGENT_HOME_PATH/.hermes/config.yaml"
install -m 0644 -o root -g root "$AGENT_HOME_SOURCE_DIR/.hermes/.env" "$AGENT_HOME_PATH/.hermes/.env"
chattr +i \
  "$AGENT_HOME_PATH/AGENTS.md" \
  "$AGENT_HOME_PATH/CLAUDE.md" \
  "$AGENT_HOME_PATH/.codex/config.toml" \
  "$AGENT_HOME_PATH/.claude/settings.json" \
  "$AGENT_HOME_PATH/.hermes/config.yaml" \
  "$AGENT_HOME_PATH/.hermes/.env"
}

write_sudoers_policy() {
cat > /etc/sudoers.d/kern-host <<'SUDOERS'
# The admin service decrypts the connected AWS key pair and passes it to the
# read-aws-account helper (STS attestation) through these environment
# variables; the per-command env_keep preserves them across sudo's env reset
# for exactly that helper and no other rule, so the Hermes launcher
# structurally never receives them. Hermes signs with a fixed routing identity
# and the proxy re-signs.
Defaults!/usr/local/lib/kern-host/read-aws-account env_keep += "KERN_BEDROCK_AWS_ACCESS_KEY_ID KERN_BEDROCK_AWS_SECRET_ACCESS_KEY"
kern-admin ALL=(root) NOPASSWD: /usr/local/lib/kern-host/reboot-host, /usr/local/lib/kern-host/run-codex-app-server, /usr/local/lib/kern-host/read-codex-account-id, /usr/local/lib/kern-host/run-claude-code, /usr/local/lib/kern-host/read-claude-account, /usr/local/lib/kern-host/run-grok, /usr/local/lib/kern-host/read-grok-account, /usr/local/lib/kern-host/run-hermes, /usr/local/lib/kern-host/run-agent-script, /usr/local/lib/kern-host/stop-agent-thread, /usr/local/lib/kern-host/read-aws-account, /usr/local/lib/kern-host/clear-agent-auth, /usr/local/lib/kern-host/read-agent-file, /usr/local/lib/kern-host/upload-agent-file, /usr/local/lib/kern-host/check-for-upgrade, /usr/local/lib/kern-host/mint-github-app-token, /usr/local/lib/kern-host/audit-github-repo, /usr/local/lib/kern-host/approve-github-push
SUDOERS
chmod 440 /etc/sudoers.d/kern-host
  # A malformed sudoers drop-in would otherwise surface only when the admin
  # service first invokes a helper; validate it now.
  visudo -c -q -f /etc/sudoers.d/kern-host
}

# Fail deploy now, not at first login, if the pinned CLIs are not executable by
# the agent user from its home directory.
assert_agent_clis() {
runuser -u kern-agent -- test -x /usr/local/bin/codex
runuser -u kern-agent -- test -x /usr/local/bin/claude
runuser -u kern-agent -- test -x /usr/local/bin/grok
runuser -u kern-agent -- test -x /usr/local/lib/hermes-venv/bin/python
runuser -u kern-tools -- test -r /usr/local/lib/kern-node/node_modules || {
  echo "Host Node dependencies are not readable by kern-tools" >&2
  exit 1
}
# Assert the decompressed binary is the pinned one and that the agent can run
# it. A non-root writer here would mean the install above fell back to the
# trampoline's agent-home path, which is exactly what it must not do.
test "$(stat -c '%U:%a' /usr/local/bin/grok)" = "root:755"
runuser -u kern-agent -- env \
  HOME="$AGENT_HOME_PATH" \
  GROK_HOME="$AGENT_HOME_PATH/.grok" \
  /usr/local/bin/grok --version | grep -qF "$GROK_CLI_VERSION"
}

# Host firewall. Root, the dedicated proxy user, and the optional cloudflared
# connector can reach their narrow external dependencies; the agent can only
# reach the loopback proxy. DNS is denied for every other non-root user because
# DNS lookups are an exfiltration channel; the proxy may resolve and connect
# only after policy allows a host. The kern-tools service gets DNS and
# HTTPS because the bundled tool packages run inside it and call their
# third-party APIs directly; the admin service holds no internet egress at all,
# so a compromised tool package cannot exfiltrate admin state, and the agent's
# fail-closed proxy path is unaffected. The kern-agent-network service communicates only over
# Unix sockets; its explicit loopback drop prevents the local policy proxy
# from becoming an indirect egress path before the broad loopback accept.
write_firewall() {
python3 - <<'PY'
import json, pathlib
config = json.loads(pathlib.Path('/tmp/kern_effective_config.json').read_text())
ssh_enabled = any(connection.get('mode') == 'ssh' for connection in config['operator_connections'])
pathlib.Path('/tmp/kern_ssh_rule').write_text('    tcp dport 22 accept\n' if ssh_enabled else '')
cloudflare_enabled = any(connection.get('mode') == 'cloudflare_tunnel' for connection in config['operator_connections'])
pathlib.Path('/tmp/kern_cloudflare_rules').write_text(
    '    meta skuid "cloudflared" udp dport 53 accept\n'
    '    meta skuid "cloudflared" tcp dport 53 accept\n'
    '    meta skuid "cloudflared" tcp dport { 443, 7844 } accept\n'
    '    meta skuid "cloudflared" udp dport 7844 accept\n'
    if cloudflare_enabled else ''
)
PY
cat > /etc/nftables.conf <<NFT
flush ruleset
table inet kern {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
$(cat /tmp/kern_ssh_rule)
  }
  chain output {
    type filter hook output priority 0; policy drop;
    meta skuid "systemd-resolve" udp dport 53 accept
    meta skuid "systemd-resolve" tcp dport 53 accept
    meta skuid "systemd-timesync" udp dport 123 accept
$(cat /tmp/kern_cloudflare_rules)
    meta skuid "kern-proxy" udp dport 53 accept
    meta skuid "kern-proxy" tcp dport 53 accept
    meta skuid "kern-proxy" tcp dport { 80, 443 } accept
    meta skuid "kern-tools" udp dport 53 accept
    meta skuid "kern-tools" tcp dport 53 accept
    meta skuid "kern-tools" tcp dport 443 accept
    udp dport 53 meta skuid != 0 drop
    tcp dport 53 meta skuid != 0 drop
    # The admin listener is loopback-only, but loopback alone is not an
    # identity boundary: an egress-capable local service could otherwise forge
    # Cloudflare's forwarding headers and spread login attempts across fake
    # source buckets. Only the two operator transports plus the trusted admin
    # and root accounts may originate a connection to the control plane.
    oif lo tcp dport @ADMIN_PORT@ meta skuid 0 accept
    oif lo tcp dport @ADMIN_PORT@ meta skuid "kern-admin" accept
    oif lo tcp dport @ADMIN_PORT@ meta skuid "kern-operator" accept
    oif lo tcp dport @ADMIN_PORT@ meta skuid "cloudflared" accept
    oif lo tcp dport @ADMIN_PORT@ drop
    oif lo tcp dport @PROXY_PORT@ meta skuid "kern-agent" accept
@AGENT_PREVIEW_NFTABLES_RULES@
    oif lo meta skuid "kern-agent" drop
    oif lo ct state established,related meta skuid "kern-workspace" accept
    oif lo meta skuid "kern-workspace" drop
    oif lo tcp dport $WORKSPACE_PORT meta skuid "kern-admin" accept
    oif lo tcp dport $WORKSPACE_PORT drop
    oif lo meta skuid "kern-agent-network" drop
    oif lo accept
    ct state established,related accept
    meta skuid 0 accept
  }
}
NFT
rm -f /tmp/kern_ssh_rule /tmp/kern_cloudflare_rules
systemctl enable --now nftables
}

install_service_units() {
# Systemd services.
# All agent runtime processes run in transient scopes under this slice (the
# run-* helpers launch them with systemd-run --scope), so the agent competes
# for resources as one cgroup instead of inside the admin API's service
# cgroup. The name deliberately uses an underscore: dashes in slice names
# encode nesting, and a nested slice's CPUWeight would be compared against
# its implicit parent's siblings, not against system.slice. As a top-level
# slice it is a direct sibling of system.slice (admin API, proxy, Postgres;
# default weight 100), so:
# - CPUWeight only matters under contention: an otherwise idle host still
#   gives the agent every core, but when host services need CPU they are
#   guaranteed about two thirds of it. A hard CPUQuota would waste idle
#   cores, so none is set.
# - The slice-wide MemoryHigh is a last-resort aggregate backstop. Each
#   transient runtime scope has a lower MemoryHigh and MemoryMax, so reclaim
#   is charged to the busy thread before unrelated runtime startups stall.
# - MemorySwapMax leaves 1G of the 6G root-volume swapfile (created above)
#   for host services. systemd 249 has no percentage form for swap limits,
#   but this script owns the swapfile size, so the absolute value cannot
#   drift independently.
# - TasksMax bounds agent threads+processes so a fork bomb cannot exhaust
#   kernel PIDs, which would stop the admin API from spawning the sudo
#   helpers (or any process) at all.
cat > /etc/systemd/system/kern_agent.slice <<'UNIT'
[Unit]
Description=Kern Agent Runtimes

[Slice]
CPUWeight=50
MemoryHigh=75%
MemoryMax=80%
MemorySwapMax=5G
TasksMax=4096
UNIT

# The workspaces backend is lower priority than the host control plane,
# while remaining free to use idle CPU.
cat > /etc/systemd/system/kern_workspace.slice <<'UNIT'
[Unit]
Description=Kern Workspaces Backend

[Slice]
CPUWeight=50
UNIT

cat > /etc/systemd/system/kern-network-proxy.service <<'UNIT'
[Unit]
Description=Kern Network Policy Proxy
After=network-online.target kern-postgres.service
Wants=network-online.target kern-postgres.service
# Never give up restarting: the proxy is the agent's only egress path and is
# fail-closed, so a crash loop must keep retrying rather than hit the default
# start-limit and stay dead.
StartLimitIntervalSec=0

[Service]
User=kern-proxy
UMask=0077
Environment=PYTHONPATH=/opt/kern-host
ExecStart=/usr/bin/python3 -m host.runtime.network_proxy.service
ExecStopPost=/usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-network-proxy
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# The dedicated tools service runs tool code and holds internet egress out of
# the admin service. It owns the agent-facing tools socket and connects to
# Postgres as the scoped kern-tools role. RuntimeDirectory stays
# world-traversable (0755) so the agent (and admin, for delegated operator
# operations) can connect; the socket peer-credential check is the authentication.
cat > /etc/systemd/system/kern-tools.service <<'UNIT'
[Unit]
Description=Kern Tools Service
After=network-online.target kern-postgres.service
Wants=network-online.target kern-postgres.service
StartLimitIntervalSec=0

[Service]
User=kern-tools
UMask=0077
RuntimeDirectory=kern-tools
RuntimeDirectoryMode=0755
Environment=PYTHONPATH=/opt/kern-host
ExecStart=/usr/bin/python3 -m host.runtime.tools.service
ExecStopPost=/usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-tools
KillMode=mixed
# A gateway request can hold its adapter lock for 40s, then bounded child
# termination can take 2s. Leave enough time for that request to unwind and
# the parent's final 2s cache-flush RPC before systemd kills the child cgroup.
TimeoutStopSec=60s
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# Read-only agent network introspection is isolated from both the egress-capable
# tools service and the privileged proxy. Its database role can read only the
# policy and network-event tables; nftables grants this uid no egress.
cat > /etc/systemd/system/kern-agent-network.service <<'UNIT'
[Unit]
Description=Kern Agent Network Introspection
After=kern-postgres.service
Wants=kern-postgres.service
StartLimitIntervalSec=0

[Service]
User=kern-agent-network
UMask=0077
RuntimeDirectory=kern-agent-network
RuntimeDirectoryMode=0755
Environment=PYTHONPATH=/opt/kern-host
ExecStart=/usr/bin/python3 -m host.runtime.agent_network.service
ExecStopPost=/usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-agent-network
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/kern-admin-api.service <<'UNIT'
[Unit]
Description=Kern Admin API
After=network-online.target kern-network-proxy.service kern-postgres.service kern-tools.service kern-agent-network.service kern-embedding.socket
Wants=network-online.target kern-network-proxy.service kern-postgres.service kern-tools.service kern-agent-network.service kern-embedding.socket
StartLimitIntervalSec=0

[Service]
User=kern-admin
UMask=0077
# kern-admin-api holds the workspace admin socket; the agent-facing
# tools socket is owned by kern-tools.service. The directory stays
# world-traversable so the Workspace service can connect; socket peer credentials
# authenticate its fixed UID.
RuntimeDirectory=kern-admin-api
RuntimeDirectoryMode=0755
# The workspace socket is connectable by its group, and it shares this
# process's fd table with the operator TCP listener, so the descriptor limit
# must not be the first resource a connection flood exhausts.
LimitNOFILE=8192
Environment=PYTHONPATH=/opt/kern-host
Environment=HOME=/mnt/kern-admin/admin-home
WorkingDirectory=/mnt/kern-admin/admin-home
ExecStart=/usr/bin/python3 -m host.runtime.admin_api.service
ExecStopPost=/usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-admin-api
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/kern-embedding.socket <<'UNIT'
[Unit]
Description=Kern Local Embedding Socket

[Socket]
ListenStream=/run/kern-embedding.sock
SocketUser=kern-embedding
SocketGroup=kern-workspace-api
SocketMode=0660
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
UNIT

cat > /etc/systemd/system/kern-embedding.service <<'UNIT'
[Unit]
Description=Kern Local Embedding Service
Requires=kern-embedding.socket
After=kern-embedding.socket

[Service]
User=kern-embedding
Group=kern-embedding
Slice=kern_workspace.slice
Environment=PYTHONPATH=/opt/kern-host
Environment=KERN_EMBEDDING_MODEL_DIR=/usr/local/share/kern-embedding-models/bge-small-en-v1.5-onnx-Q
Environment=HF_HUB_OFFLINE=1
Environment=OMP_NUM_THREADS=1
ExecStart=/usr/local/lib/kern-embedding-venv/bin/python -m host.runtime.embeddings.service
ExecStopPost=/usr/bin/env PYTHONPATH=/opt/kern-host /usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-embedding
NoNewPrivileges=yes
PrivateNetwork=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
Nice=10
CPUWeight=25
IOWeight=25
MemoryMax=1G
TasksMax=64

UNIT

cat > /etc/systemd/system/kern-host-errors.service <<'UNIT'
[Unit]
Description=Kern Host Diagnostics Journal Collector
After=systemd-journald.service
Wants=kern-postgres.service
StartLimitIntervalSec=0

[Service]
User=kern-admin
SupplementaryGroups=systemd-journal
UMask=0077
Environment=PYTHONPATH=/opt/kern-host
ExecStart=/usr/bin/python3 -m host.runtime.host_diagnostics_collector.collector
ExecStopPost=/usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-host-errors
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/kern-workspace.service <<'UNIT'
[Unit]
Description=Kern Workspace
After=network-online.target kern-admin-api.service kern-postgres.service kern-embedding.socket
Wants=network-online.target kern-admin-api.service kern-postgres.service kern-embedding.socket
StartLimitIntervalSec=0

[Service]
User=kern-workspace
Slice=kern_workspace.slice
UMask=0077
RuntimeDirectory=kern-workspace
RuntimeDirectoryMode=0755
Environment=PYTHONPATH=/opt/kern-host
WorkingDirectory=/opt/kern-host
ExecStart=/usr/bin/python3 -m host.runtime.workspace.service
ExecStopPost=/usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-workspace
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

if [ "$cloudflare_connection_count" -gt 0 ]; then
  cat > /etc/systemd/system/kern-cloudflared.service <<'UNIT'
[Unit]
Description=Kern Cloudflare Tunnel
After=network-online.target kern-admin-api.service
Wants=network-online.target kern-admin-api.service
StartLimitIntervalSec=0

[Service]
User=cloudflared
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate run --token-file /etc/kern/cloudflared.token
ExecStopPost=/usr/bin/env PYTHONPATH=/opt/kern-host /usr/bin/python3 -m host.runtime.core.host_errors_service_exit kern-cloudflared
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
fi
}

start_services() {
systemctl daemon-reload
systemctl enable --now kern-network-proxy.service
systemctl enable --now kern-host-errors.service
systemctl enable --now kern-tools.service
systemctl enable --now kern-agent-network.service
systemctl enable --now kern-embedding.socket
systemctl enable --now kern-admin-api.service
systemctl enable kern-workspace.service
systemctl start kern-workspace.service
if [ "$cloudflare_connection_count" -gt 0 ]; then
  systemctl enable --now kern-cloudflared.service
  for attempt in $(seq 1 30); do
    if systemctl is-active --quiet kern-cloudflared.service; then
      break
    fi
    sleep 2
  done
  if ! systemctl is-active --quiet kern-cloudflared.service; then
    journalctl -u kern-cloudflared.service --no-pager -n 80 >&2 || true
    echo "cloudflared service did not become active" >&2
    exit 1
  fi
  # The admin login is the authentication boundary; the tunnel is transport and
  # Cloudflare edge protection only. A healthy tunnel reaches the admin API,
  # which denies the unauthenticated probe with 401. A 200 would mean the origin
  # answered with no login required and fails the deploy. A legacy deployment
  # may still sit behind a Cloudflare Access policy that answers 302/403 before
  # the origin; the origin still enforces the login, so that also passes.
  cloudflare_hostname="$(cat /etc/kern/cloudflare_hostname)"
  # HTTPS must reach an authentication gate: the admin API denies the
  # unauthenticated probe with 401, and a legacy host still behind a Cloudflare
  # Access policy answers 302/403 before the origin. Only a 200 (or no gate at
  # all) fails, meaning the origin answered with no login required.
  cloudflare_status=""
  for attempt in $(seq 1 30); do
    cloudflare_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "https://${cloudflare_hostname}/v1/health" || true)"
    case "$cloudflare_status" in
      401|302|403)
        break
        ;;
    esac
    sleep 5
  done
  case "$cloudflare_status" in
    401)
      echo "Cloudflare tunnel probe for ${cloudflare_hostname} reached the admin API login gate (401)"
      ;;
    302|403)
      echo "Cloudflare tunnel probe for ${cloudflare_hostname} returned ${cloudflare_status} (legacy Cloudflare Access gate present)"
      ;;
    *)
      echo "Cloudflare hostname ${cloudflare_hostname} did not return an authentication gate over HTTPS; last status: ${cloudflare_status:-none}" >&2
      echo "Check that the tunnel public hostname points to http://localhost:@ADMIN_PORT@ and that the admin API is running." >&2
      exit 1
      ;;
  esac
  # Cleartext HTTP must be redirected to HTTPS, never served: the admin login and
  # its session cookie must never cross the wire in the clear. The edge ("Always
  # Use HTTPS") or the origin itself answers a non-HTTPS request with a redirect.
  http_status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://${cloudflare_hostname}/v1/health" || true)"
  case "$http_status" in
    301|302|307|308)
      echo "Cloudflare hostname ${cloudflare_hostname} redirects HTTP to HTTPS (${http_status})"
      ;;
    *)
      echo "Cloudflare hostname ${cloudflare_hostname} did not redirect HTTP to HTTPS; last status: ${http_status:-none}" >&2
      echo "Enable 'Always Use HTTPS' for the zone so cleartext requests never reach the admin login." >&2
      exit 1
      ;;
  esac
fi
}

# Independent end-of-deploy verification: pinned identities, path ownership,
# service sockets, loopback listeners, active units, database peer auth, and
# live firewall probes in both directions (allowed paths connect, denied
# paths drop) must match the constants the services themselves run with. Any
# mismatch fails the deploy here, before the staged secrets are dropped.
verify_deployment() {
  echo "== verifying deployed state =="
  local cloudflare_flag=no
  if [ "$cloudflare_connection_count" -gt 0 ]; then
    cloudflare_flag=yes
  fi
  env PYTHONPATH=/opt/kern-host python3 -m host.bootstrap.verify_deploy --cloudflare "$cloudflare_flag"
}

# Provisioning is almost done: capture the non-secret target version, then
# drop the single-use deploy key and staged files after all service checks pass.
finalize_deploy() {
target_version="$(payload_value operation.target_version)"
rm -f /home/kern-operator/.ssh/authorized_keys2
rm -f /tmp/kern_payload.json /tmp/kern_effective_config.json /tmp/kern-host-code.tar.gz /tmp/kern_bootstrap.sh

# The admin disk version is authoritative for preserved state. Advance it only
# after the root-volume install and service setup have succeeded.
KERN_TARGET_VERSION="$target_version" python3 - <<'PY'
import json, os, pathlib, time

version_path = pathlib.Path('/mnt/kern-admin/admin-state/version.json')
version_path.write_text(json.dumps({
    'version': os.environ['KERN_TARGET_VERSION'],
    'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
}, indent=2, sort_keys=True) + '\n')
PY
chown kern-admin:kern-admin /mnt/kern-admin/admin-state/version.json
chmod 600 /mnt/kern-admin/admin-state/version.json
}

main() {
  mount_durable_volumes
  provision_service_accounts
  sanitize_durable_paths
  enforce_version_gate
  install_runtime_code
  install_system_packages
  setup_postgres
  migrate_admin_state_and_write_config
  configure_operator_ssh
  install_agent_clis
  configure_cloudflared
  write_codex_policy
  write_grok_policy
  harden_base_os
  setup_proxy_ca
  install_sudo_helpers
  apply_durable_ownership
  install_agent_home_files
  write_sudoers_policy
  assert_agent_clis
  write_firewall
  install_service_units
  start_services
  verify_deployment
  finalize_deploy
}

main
