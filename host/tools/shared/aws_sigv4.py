"""Small standard-library AWS Signature Version 4 helpers for tool packages."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import hmac


@dataclass(frozen=True)
class SignedRequest:
    url: str
    headers: dict[str, str]
    body: bytes


def signing_key(secret_access_key: str, date_stamp: str, region: str, service: str) -> bytes:
    key = hmac.new(f"AWS4{secret_access_key}".encode(), date_stamp.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, service.encode(), hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


def sign_post(
    *,
    host: str,
    region: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
    now: datetime.datetime | None = None,
) -> SignedRequest:
    """Sign one ``POST https://<host>/`` request with lowercase headers."""
    when = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = when.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": content_type,
        "host": host,
        "x-amz-date": amz_date,
        **(extra_headers or {}),
    }
    signed_header_names = ";".join(sorted(headers))
    canonical_headers = "".join(
        f"{name}:{headers[name].strip()}\n" for name in sorted(headers)
    )
    canonical_request = "\n".join(
        ("POST", "/", "", canonical_headers, signed_header_names, payload_hash)
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    signature = hmac.new(
        signing_key(secret_access_key, date_stamp, region, service),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    request_headers = dict(headers)
    del request_headers["host"]  # urllib sets Host from the URL
    request_headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    return SignedRequest(
        url=f"https://{host}/", headers=request_headers, body=body
    )
