#!/usr/bin/env bash
set -euo pipefail
# Reads the Grok login's account identity for the admin API, which has no
# access to the agent home itself. It runs as kern-agent (never as root), but
# the root runuser parent cannot rely on the admin service's timeout to stop a
# child blocked on an agent-planted special file. The final path is therefore
# opened without following symlinks, in nonblocking mode, then bounded after an
# fstat regular-file check.
#
# Exit 0 with a JSON object on stdout, 2 when no login exists, 1 on failure.
mode="read"
if [[ $# -eq 1 && "$1" == "--attest" ]]; then
  mode="attest"
elif [[ $# -ne 0 ]]; then
  echo "usage: read-grok-account [--attest]" >&2
  exit 64
fi

if [[ "${mode}" == "attest" ]]; then
  # Attestation asks xAI who the agent's current token belongs to, so the
  # identity is bound to the token by the provider instead of by a claim this
  # host merely decodes. It runs as root for the same reason Claude's does:
  # the agent uid reaches only the local proxy, whose account guard would
  # reject the very token being attested, and the admin uid has no egress,
  # while root egress is open. Root needs the raw token to make the request --
  # the admin caller only ever holds its sha256 -- so root opens the file
  # itself, with the same hardening as the unprivileged read below. The token
  # never leaves this process.
  GROK_HOME=/mnt/kern-agent/agent-home/.grok \
  exec /usr/bin/python3 - <<'ATTEST'
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.request

ISSUER = "https://auth.x.ai"
MAX_AUTH_BYTES = 256 * 1024
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# The identity endpoint on the subscription chat proxy. Verified against a
# live Grok 1.0.5 personal login: bearer authentication alone is sufficient,
# and the response is a flat object carrying the fields below.
IDENTITY_URL = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"


def first_string(value, keys):
    if not isinstance(value, dict):
        return None
    for key in keys:
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip()
    return None


def read_auth():
    path = Path(os.environ["GROK_HOME"], "auth.json")
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            return {}
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(MAX_AUTH_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_AUTH_BYTES:
        print("the Grok auth file is unexpectedly large", file=sys.stderr)
        sys.exit(1)
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


auth = read_auth()
sessions = [
    value for value in auth.values()
    if isinstance(value, dict)
    and (first_string(value, ("oidc_issuer",)) or "").rstrip("/") == ISSUER
    and first_string(value, ("key",))
]
if len(sessions) > 1:
    print("the Grok auth file holds more than one xAI session", file=sys.stderr)
    sys.exit(1)
access_token = first_string(sessions[0], ("key",)) if sessions else None
if not access_token:
    print("no Grok credentials to attest", file=sys.stderr)
    sys.exit(1)

access_token_sha256 = hashlib.sha256(access_token.encode()).hexdigest()
request = urllib.request.Request(
    IDENTITY_URL, headers={"Authorization": "Bearer " + access_token}
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        profile = json.loads(response.read(MAX_AUTH_BYTES + 1))
except urllib.error.HTTPError as exc:
    print(f"the xAI identity endpoint rejected the token: HTTP {exc.code}", file=sys.stderr)
    sys.exit(1)
except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
    print(f"could not reach the xAI identity endpoint: {exc}", file=sys.stderr)
    sys.exit(1)

principal_type = first_string(profile, ("principalType",))
if principal_type == "Team":
    account_id = first_string(profile, ("principalId",))
elif principal_type == "User":
    account_id = first_string(profile, ("userId",))
else:
    account_id = None
email = first_string(profile, ("email",))
if not account_id:
    print("the xAI identity response carried no account id", file=sys.stderr)
    sys.exit(1)

result = {"access_token_sha256": access_token_sha256, "account_id": account_id}
if email:
    result["email"] = email
print(json.dumps(result, sort_keys=True))
ATTEST
fi

exec /usr/sbin/runuser -u kern-agent -- env \
  HOME=/mnt/kern-agent/agent-home \
  GROK_HOME=/mnt/kern-agent/agent-home/.grok \
  /usr/bin/python3 - <<'PY'
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

# The account id Kern anchors and the proxy pins is taken from the access
# token's own signed claims, never from the plain user_id field beside them in
# the (agent-writable) auth file. This mirrors how the proxy guard reads it, and
# how the Codex helper takes the provider-signed chatgpt_account_id claim: a
# tampered claim breaks the signature xAI itself verifies, so only a genuine
# token of that account both passes here and authenticates upstream.
#
# xAI's own token handling supplies the id under one of two claims: a User
# principal carries it as "sub", and a Team principal as "principal_id" (a
# Team principal's user id is its principal id). The signed principal type
# decides which claim is authoritative; taking the first populated claim would
# anchor a team login to its distinct subject instead of its team principal.
PRINCIPAL_TYPE_KEYS = ("principal_type", "principalType")
PRINCIPAL_ID_KEYS = ("principal_id", "principalId")
# auth.json is a map of OIDC sessions keyed by "<issuer>::<client_id>", e.g.
# "https://auth.x.ai::b1a00492-...". The key is therefore not a fixed name to
# look up; sessions are selected by their issuer field instead. Each session
# holds the access token under "key" (not "access_token"), alongside
# refresh_token, user_id, principal_id, principal_type, team_id and email.
# Verified against grok 1.0.5.
ISSUER = "https://auth.x.ai"
ISSUER_KEYS = ("oidc_issuer",)
TOKEN_KEYS = ("key",)
USER_ID_KEYS = ("user_id", "userId")
MAX_AUTH_BYTES = 256 * 1024
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def jwt_payload(token):
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def candidates(auth):
    """The xAI OIDC sessions in the auth document.

    Sessions issued by anything other than xAI are skipped rather than trusted:
    the CLI can hold several, and only xAI's own is the account this host pins.

    The issuer must be present and match. Accepting an issuer-less object would
    let anything shaped like a session qualify, and this file is agent-writable
    -- the point of reading it here is that a claim survives being read from an
    untrusted place, which only holds while the candidate set is exactly xAI's.
    """
    if not isinstance(auth, dict):
        return
    for value in auth.values():
        if not isinstance(value, dict):
            continue
        issuer = first_string(value, ISSUER_KEYS)
        if issuer is None or issuer.rstrip("/") != ISSUER:
            continue
        yield value


def first_string(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


grok_home = Path(os.environ.get("GROK_HOME") or (Path.home() / ".grok"))
auth_path = grok_home / "auth.json"
try:
    auth_fd = os.open(auth_path, os.O_RDONLY | NOFOLLOW | NONBLOCK)
except FileNotFoundError:
    sys.exit(2)
except OSError as exc:
    print(f"could not read the Grok auth file: {exc}", file=sys.stderr)
    sys.exit(1)
try:
    info = os.fstat(auth_fd)
    if not stat.S_ISREG(info.st_mode):
        print("the Grok auth path is not a regular file", file=sys.stderr)
        sys.exit(1)
    with os.fdopen(auth_fd, "rb") as handle:
        auth_fd = -1
        raw = handle.read(MAX_AUTH_BYTES + 1)
finally:
    if auth_fd >= 0:
        os.close(auth_fd)
if len(raw) > MAX_AUTH_BYTES:
    print("the Grok auth file is unexpectedly large", file=sys.stderr)
    sys.exit(1)
try:
    auth = json.loads(raw)
except (json.JSONDecodeError, UnicodeDecodeError):
    print("the Grok auth file is not valid JSON", file=sys.stderr)
    sys.exit(1)

# Every xAI session carrying a token, not the first one found. Taking the
# first lets an agent prepend a session of its own and have it read instead of
# the one Grok actually authenticates with -- same account id, its own email.
# One session is the only state that says which credential is in use, so
# anything else is an inconsistent file and fails closed, exactly as a
# disagreeing user id does below.
authenticated = [
    (candidate, first_string(candidate, TOKEN_KEYS))
    for candidate in candidates(auth)
    if first_string(candidate, TOKEN_KEYS)
]
if len(authenticated) > 1:
    print("the Grok auth file holds more than one xAI session", file=sys.stderr)
    sys.exit(1)
session, access_token = authenticated[0] if authenticated else (None, None)
if not access_token:
    # A file with no access token is an incomplete or logged-out state, not a
    # failure: report it the same way as a missing file so the runtime settles
    # on awaiting_login rather than error.
    sys.exit(2)

claims = jwt_payload(access_token)
principal_type = first_string(claims, PRINCIPAL_TYPE_KEYS) if isinstance(claims, dict) else None
subject_id = first_string(claims, ("sub",)) if isinstance(claims, dict) else None
principal_id = first_string(claims, PRINCIPAL_ID_KEYS) if isinstance(claims, dict) else None
normalized_principal_type = principal_type.casefold() if principal_type else None
if normalized_principal_type == "team":
    account_id = principal_id
    reported_user_id = first_string(session, PRINCIPAL_ID_KEYS) if session else None
elif normalized_principal_type == "user":
    account_id = subject_id
    reported_user_id = first_string(session, USER_ID_KEYS) if session else None
elif principal_type:
    print(f"the Grok access token has an unsupported principal type: {principal_type}", file=sys.stderr)
    sys.exit(1)
elif subject_id == principal_id:
    # Older personal tokens may omit principal_type. Their two account claims
    # are identical, so accepting that one unambiguous value is safe.
    account_id = subject_id
    reported_user_id = first_string(session, USER_ID_KEYS + PRINCIPAL_ID_KEYS) if session else None
else:
    account_id = None
    reported_user_id = None
if not account_id:
    print("the Grok access token carries no unambiguous account claim", file=sys.stderr)
    sys.exit(1)

# The plain user_id is not trusted as the source of the id, but when it is
# present and disagrees with the signed claim the file is inconsistent — fail
# closed rather than choose one.
if reported_user_id and reported_user_id != account_id:
    print("the Grok auth file names a different account than its token claims", file=sys.stderr)
    sys.exit(1)

result = {
    "account_id": account_id,
    "access_token_sha256": hashlib.sha256(access_token.encode()).hexdigest(),
}
print(json.dumps(result, sort_keys=True))
PY
