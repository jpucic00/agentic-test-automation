"""
Optional mTLS client-certificate support for the step0 verification scripts.

When the corporate LLM gateway / Jira / GitLab requires mutual TLS, set ONE of
the following in .env:

    MTLS_PKCS12_FILE        path to a PKCS#12 bundle (.p12 or .pfx — same format)
    MTLS_PKCS12_PASSWORD    password protecting that bundle

OR (alternative if you have separate PEM files instead of a single .pfx):

    MTLS_CERT_FILE          PEM client cert
    MTLS_KEY_FILE           PEM client key
    MTLS_KEY_PASSWORD       passphrase, if the key file is encrypted

And optionally for server-side trust when the gateway uses a private CA:

    SSL_CERT_FILE           corporate root CA bundle (PEM)

Proxy behavior:

    USE_HTTP_PROXY          if "true"/"1"/"yes"/"on", httpx honors the
                            environment's HTTP(S)_PROXY / NO_PROXY (trust_env=True).
                            DEFAULT is direct: the scripts ignore the env proxy and
                            connect straight to the endpoint (reached over the VPN
                            on the company laptop — this does NOT bypass the VPN).
                            Routing through an env-configured proxy is a common
                            cause of "server disconnected without sending a
                            response" even when the handshake succeeds — a direct
                            `curl` works while the same request proxied drops.

Usage (matches the platform team's reference example for this gateway):

    from openai import DefaultHttpxClient, OpenAI
    import _mtls

    # Always build an explicit client so the proxy policy (and CA bundle) apply
    # even when no client cert is configured.
    http_kwargs = {"trust_env": _mtls.get_trust_env(), "verify": _mtls.get_verify_arg()}
    cert = _mtls.get_cert_arg()
    if cert is not None:
        http_kwargs["cert"] = cert
    client = OpenAI(base_url=..., api_key=..., http_client=DefaultHttpxClient(**http_kwargs))

`get_cert_arg()` returns a value suitable for `httpx`'s native `cert=` parameter:
  - None: no mTLS configured (caller uses defaults)
  - str: path to a combined PEM file (cert chain + key)
  - 2-tuple: (cert_path, key_path)
  - 3-tuple: (cert_path, key_path, key_password)

For .pfx / .p12 inputs the bundle is decrypted in-memory via the `cryptography`
library, then written as a single combined PEM to a 0600 temp file (cert chain
followed by unencrypted key — same shape as `openssl pkcs12 -nodes -out`),
with atexit cleanup. Key material lives unencrypted in /tmp for the duration
of the script — acceptable for a short-lived verification run.

`load_dotenv()` must run BEFORE the helpers are called.
"""
from __future__ import annotations

import atexit
import os
import stat
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import requests

CertArg = str | tuple[str, str] | tuple[str, str, str]


def get_cert_arg() -> CertArg | None:
    """Return a value for `httpx.Client(cert=...)` based on MTLS_* env vars."""
    p12_file = os.environ.get("MTLS_PKCS12_FILE")
    if p12_file:
        password = os.environ.get("MTLS_PKCS12_PASSWORD") or None
        return _decode_pkcs12_to_pem(p12_file, password)

    cert_file = os.environ.get("MTLS_CERT_FILE")
    if cert_file:
        key_file = os.environ.get("MTLS_KEY_FILE") or None
        key_pw = os.environ.get("MTLS_KEY_PASSWORD") or None
        if key_file and key_pw:
            return (cert_file, key_file, key_pw)
        if key_file:
            return (cert_file, key_file)
        return cert_file

    return None


def get_verify_arg() -> str | bool:
    """Return `verify=` for httpx.Client. Points at the corp CA bundle if
    SSL_CERT_FILE is set, else True (httpx uses certifi's default bundle)."""
    return os.environ.get("SSL_CERT_FILE") or True


def get_trust_env() -> bool:
    """Whether httpx may read proxy/CA/netrc settings from the environment.

    Defaults to False: these verification scripts connect DIRECTLY to the gateway
    (and Jira) and IGNORE the environment's HTTP(S)_PROXY / NO_PROXY vars. On the
    company laptop the endpoint is reached over the VPN; routing the request
    through the env-configured proxy is what dropped it ("server disconnected
    without sending a response" even though the TLS/mTLS handshake succeeds — a
    direct `curl` works while the same request through the proxy drops
    identically). This does NOT bypass the VPN, which is still required for
    connectivity. Set USE_HTTP_PROXY=true to opt back into the environment proxy
    (e.g. when the endpoint is only reachable THROUGH a proxy). The CA bundle is
    unaffected — it is always passed explicitly via get_verify_arg()."""
    return os.environ.get("USE_HTTP_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}


def apply_requests_policy(session: requests.Session) -> None:
    """Apply the gateway proxy/CA/mTLS policy to an existing ``requests.Session`` in
    place — for clients that own their session and don't accept one (python-gitlab
    exposes ``gl.session``).

    Mirrors the httpx policy used for the gateway: ``trust_env`` follows
    ``USE_HTTP_PROXY`` (default False → the env ``HTTP(S)_PROXY`` is ignored and the
    call goes DIRECTLY over the VPN — the same fix that stopped the gateway dropping
    with "server disconnected without sending a response"). The corp CA / client cert
    are applied explicitly so they survive ``trust_env=False`` (which would otherwise
    drop ``REQUESTS_CA_BUNDLE``)."""
    session.trust_env = get_trust_env()
    verify = get_verify_arg()
    if verify is not True:
        session.verify = verify
    cert = get_cert_arg()
    if cert is not None:
        if isinstance(cert, tuple) and len(cert) == 3:
            # requests' cert= takes a path or (cert, key) 2-tuple only — not an
            # encrypted-key 3-tuple. The httpx gateway path supports it; the
            # requests-based Xray/GitLab clients need PKCS#12 or an unencrypted key.
            raise ValueError(
                "mTLS with an encrypted PEM key (MTLS_KEY_PASSWORD) isn't supported for "
                "the requests-based Xray/GitLab clients. Use MTLS_PKCS12_FILE (decoded "
                "to a combined PEM) or an unencrypted MTLS_KEY_FILE instead."
            )
        session.cert = cert


def build_requests_session() -> requests.Session:
    """A new ``requests.Session`` carrying the gateway proxy/CA/mTLS policy, for the
    ``requests``-based clients that accept ``session=`` (atlassian-python-api's Jira).

    Without it those clients use ``requests``' default ``trust_env=True`` and silently
    honor the env ``HTTP(S)_PROXY`` — the exact routing that dropped the gateway. See
    ``apply_requests_policy`` for the policy details."""
    import requests

    session = requests.Session()
    apply_requests_policy(session)
    return session


def describe_trust_env() -> str:
    """One-line proxy-mode summary for pre-flight prints."""
    if get_trust_env():
        return "env proxy honored (USE_HTTP_PROXY=true)"
    return "direct — ignoring env HTTP(S)_PROXY (set USE_HTTP_PROXY=true to use it)"


def describe() -> str:
    """One-line summary for pre-flight prints. The password is never logged."""
    parts: list[str] = []
    if os.environ.get("MTLS_PKCS12_FILE"):
        pwd_state = "set" if os.environ.get("MTLS_PKCS12_PASSWORD") else "MISSING"
        parts.append(f"PKCS#12={os.environ['MTLS_PKCS12_FILE']} (pwd: {pwd_state})")
    if os.environ.get("MTLS_CERT_FILE"):
        parts.append(f"CERT={os.environ['MTLS_CERT_FILE']}")
        if os.environ.get("MTLS_KEY_FILE"):
            parts.append(f"KEY={os.environ['MTLS_KEY_FILE']}")
    if os.environ.get("SSL_CERT_FILE"):
        parts.append(f"SSL_CERT_FILE={os.environ['SSL_CERT_FILE']}")
    return ", ".join(parts) if parts else "(none — httpx defaults)"


def _decode_pkcs12_to_pem(p12_path: str, password: str | None) -> str:
    """Decode .pfx / .p12 → single combined PEM file (cert chain + key) in a
    0600 temp file, return its path. Same shape as `openssl pkcs12 -nodes -out`."""
    # Lazy import: only paid by callers that actually use PKCS#12.
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )
    from cryptography.hazmat.primitives.serialization.pkcs12 import (
        load_key_and_certificates,
    )

    if not os.path.isfile(p12_path):
        raise FileNotFoundError(
            f"MTLS_PKCS12_FILE not found at {p12_path!r}. "
            "Path must be ABSOLUTE (no ~ — .env doesn't expand tilde)."
        )
    with open(p12_path, "rb") as f:
        p12_data = f.read()
    if not p12_data:
        raise RuntimeError(f"{p12_path}: file is empty")

    try:
        key, cert, additional = load_key_and_certificates(
            p12_data, password.encode() if password else None
        )
    except ValueError as e:
        # cryptography raises ValueError with "Could not deserialize key data"
        # or "Invalid password" — both indicate a wrong/missing password or a
        # malformed PKCS#12 bundle. Re-raise with a script-friendly message.
        if "password" in str(e).lower() or "deserialize" in str(e).lower():
            raise ValueError(
                f"Cannot decrypt {p12_path}: wrong MTLS_PKCS12_PASSWORD or the "
                f"file is not a valid PKCS#12 bundle. (Underlying: {e})"
            ) from e
        raise

    if cert is None or key is None:
        raise RuntimeError(f"{p12_path}: PKCS#12 must contain both a cert and a key")

    cert_pem = cert.public_bytes(Encoding.PEM)
    for ca in additional or ():
        cert_pem += ca.public_bytes(Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )

    return _write_temp_secret(cert_pem + key_pem, suffix=".pem")


def _write_temp_secret(data: bytes, *, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        os.write(fd, data)
    finally:
        os.close(fd)
    atexit.register(_safe_unlink, path)
    return path


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
