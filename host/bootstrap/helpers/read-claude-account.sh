#!/usr/bin/env bash
set -euo pipefail

mode="read"
expected_token_sha256=""
while [[ "$#" -ge 1 ]]; do
  case "$1" in
    --attest)
      mode="attest"
      shift
      ;;
    --expected-token-sha256)
      if [[ "$#" -lt 2 ]]; then
        echo "usage: read-claude-account [--attest [--expected-token-sha256 <sha256>]]" >&2
        exit 2
      fi
      expected_token_sha256="$2"
      shift 2
      ;;
    *)
      echo "usage: read-claude-account [--attest [--expected-token-sha256 <sha256>]]" >&2
      exit 2
      ;;
  esac
done
if [[ -n "${expected_token_sha256}" && "${mode}" != "attest" ]]; then
  echo "usage: read-claude-account [--attest [--expected-token-sha256 <sha256>]]" >&2
  exit 2
fi

if [[ "${mode}" == "attest" ]]; then
  # Attestation asks api.anthropic.com/api/oauth/profile who the agent's
  # current OAuth token belongs to, so the account identity is bound to the
  # token by the provider instead of agent-writable metadata. It runs as root
  # on purpose: the agent uid can only reach the local proxy (whose account
  # guard rejects a just-rotated token) and the admin uid has no egress at
  # all, while root egress is open. Root needs the *raw* access token to make
  # the provider request (the admin caller only ever holds its sha256), so the
  # token cannot be piped in from an unprivileged pass without exposing the
  # secret to the no-egress admin uid — root must open the file itself. That
  # read is hardened below (see read_agent_credential). The token never leaves
  # this process.
  EXPECTED_TOKEN_SHA256="${expected_token_sha256}" exec /usr/bin/python3 - <<'PY'
import errno
import hashlib
import json
import os
import stat
import sys
import urllib.error
import urllib.request

# The agent owns /mnt/kern-agent/agent-home/.claude (0700) and .credentials.json
# is NOT among the chattr +i managed files, so the agent can swap that name for
# a symlink, a FIFO, /dev/zero, or a root-only path between any unprivileged
# pre-read and this root read. We therefore open the credential exactly like the
# sibling read-agent-file root helper rather than json.loads(path.read_text()):
#   - walk down real directory fds with O_NOFOLLOW (a symlinked component or a
#     final-name symlink fails with ELOOP, so root can't be redirected at a
#     root-only path to leak an existence/size oracle);
#   - open the file with O_NONBLOCK (a FIFO returns immediately instead of
#     blocking the root helper forever — the kern-admin parent cannot signal a
#     root child, so the subprocess timeout would otherwise be inert);
#   - re-check S_ISREG on the *opened* fd via fstat (rejects /dev/zero, FIFOs,
#     and any other non-regular file that slipped past the name check);
#   - cap the read to a small bound (a real credential file is a few KB, so this
#     defeats an unbounded /dev/zero-style allocation outside the agent cgroup).
AGENT_HOME = "/mnt/kern-agent/agent-home"
CREDENTIAL_PARTS = (".claude", ".credentials.json")
MAX_CREDENTIAL_BYTES = 256 * 1024
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def read_agent_credential():
    try:
        dir_fd = os.open(AGENT_HOME, os.O_RDONLY | DIRECTORY | NOFOLLOW)
        try:
            for part in CREDENTIAL_PARTS[:-1]:
                info = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise OSError(errno.ELOOP, "symlink in credential path")
                next_fd = os.open(
                    part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=dir_fd
                )
                os.close(dir_fd)
                dir_fd = next_fd
            file_fd = os.open(
                CREDENTIAL_PARTS[-1],
                os.O_RDONLY | NOFOLLOW | NONBLOCK,
                dir_fd=dir_fd,
            )
        finally:
            os.close(dir_fd)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        # ELOOP: O_NOFOLLOW refused a symlink component/target (swap attack).
        if exc.errno == errno.ELOOP:
            return {}
        raise
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            # /dev/zero, a FIFO, or any other non-regular swap target.
            return {}
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = -1
            data = handle.read(MAX_CREDENTIAL_BYTES + 1)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
    if len(data) > MAX_CREDENTIAL_BYTES:
        print("Claude credential file is unexpectedly large", file=sys.stderr)
        sys.exit(1)
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


credentials = read_agent_credential()
tokens = credentials.get("claudeAiOauth")
access_token = tokens.get("accessToken") if isinstance(tokens, dict) else None
if not isinstance(access_token, str) or not access_token.strip():
    print("no Claude OAuth credentials to attest", file=sys.stderr)
    sys.exit(1)
access_token = access_token.strip()
access_token_sha256 = hashlib.sha256(access_token.encode()).hexdigest()
expected_token_sha256 = os.environ.get("EXPECTED_TOKEN_SHA256", "").strip()
if expected_token_sha256 and access_token_sha256 != expected_token_sha256:
    print("Claude OAuth token changed before attestation", file=sys.stderr)
    sys.exit(1)

request = urllib.request.Request(
    "https://api.anthropic.com/api/oauth/profile",
    headers={"Authorization": "Bearer " + access_token},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        profile = json.load(response)
except urllib.error.HTTPError as exc:
    print(f"Claude profile endpoint rejected the token: HTTP {exc.code}", file=sys.stderr)
    sys.exit(1)
except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
    print(f"could not reach the Claude profile endpoint: {exc}", file=sys.stderr)
    sys.exit(1)

account = profile.get("account") if isinstance(profile, dict) else None
organization = profile.get("organization") if isinstance(profile, dict) else None
account_uuid = account.get("uuid") if isinstance(account, dict) else None
if not isinstance(account_uuid, str) or not account_uuid.strip():
    print("Claude profile response has no account uuid", file=sys.stderr)
    sys.exit(1)

result = {
    "access_token_sha256": access_token_sha256,
    "account_uuid": account_uuid.strip(),
}
email = account.get("email") if isinstance(account, dict) else None
if isinstance(email, str) and email.strip():
    result["email"] = email.strip()
organization_uuid = organization.get("uuid") if isinstance(organization, dict) else None
if isinstance(organization_uuid, str) and organization_uuid.strip():
    result["organization_uuid"] = organization_uuid.strip()
print(json.dumps(result, sort_keys=True))
PY
fi

exec /usr/sbin/runuser -u kern-agent -- env HOME=/mnt/kern-agent/agent-home CLAUDE_CONFIG_DIR=/mnt/kern-agent/agent-home/.claude /usr/bin/python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

home = Path.home()
config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))


def load_first(paths):
    for path in paths:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            continue
    return {}


config = load_first([
    config_dir / ".claude.json",
    Path(str(config_dir) + ".json"),
    config_dir.parent / ".claude.json",
    home / ".claude.json",
])
credentials = load_first([
    config_dir / ".credentials.json",
    home / ".claude" / ".credentials.json",
])

oauth = config.get("oauthAccount")
tokens = credentials.get("claudeAiOauth")
if not isinstance(tokens, dict):
    sys.exit(1)
access_token = tokens.get("accessToken")
if not isinstance(access_token, str) or not access_token.strip():
    sys.exit(1)

result = {
    "access_token_sha256": hashlib.sha256(access_token.strip().encode()).hexdigest(),
}
if isinstance(oauth, dict):
    account_id = oauth.get("accountUuid")
    organization_id = oauth.get("organizationUuid")
    email = oauth.get("emailAddress")
    if isinstance(account_id, str) and account_id.strip():
        result["account_id"] = account_id.strip()
    if isinstance(organization_id, str) and organization_id.strip():
        result["organization_id"] = organization_id.strip()
    if isinstance(email, str) and email.strip():
        result["email"] = email.strip()
print(json.dumps(result, sort_keys=True))
PY
