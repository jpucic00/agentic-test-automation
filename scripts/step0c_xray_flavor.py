"""
Step 0c — determine Xray flavor (Cloud vs Server/Data Center) and the
"test steps" custom field ID for the target Jira project.

The Xray client (Phase 1.A) branches on flavor: Cloud uses GraphQL, Server/DC
uses REST plus a custom field that holds the steps. The custom field ID
differs per tenant (commonly customfield_10100 or customfield_10200), so we
identify it now from a real test case.

Reads from .env:
  JIRA_BASE_URL               — e.g. https://yourcompany.atlassian.net (Cloud) or self-hosted
  JIRA_EMAIL                  — Cloud: account email; Server/DC: username
  JIRA_TOKEN                  — Cloud: API token; Server/DC: PAT or password
  XRAY_IS_CLOUD               — explicit hint; the script also auto-detects
  MTLS_PKCS12_FILE/PASSWORD   — optional: mTLS client cert as a .pfx/.p12 bundle
  MTLS_CERT_FILE / KEY_FILE   — optional: same as above but separate PEM files
  SSL_CERT_FILE               — optional: corporate root CA bundle

Must run on the company laptop.

Run:
  uv run python scripts/step0c_xray_flavor.py --issue-key QA-1234
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _mtls  # noqa: E402

if not all(os.environ.get(k) for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN")):
    print("[fail] JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN must be set in .env")
    sys.exit(2)

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
XRAY_IS_CLOUD_HINT = os.environ.get("XRAY_IS_CLOUD", "").lower() == "true"

try:
    _cert = _mtls.get_cert_arg()
except Exception as e:
    print(f"[fail] mTLS setup failed: {type(e).__name__}: {e}")
    sys.exit(2)

_client_kwargs: dict = {"timeout": 15.0}
if _cert is not None:
    _client_kwargs["cert"] = _cert
    _client_kwargs["verify"] = _mtls.get_verify_arg()
HTTP = httpx.Client(**_client_kwargs)


def detect_flavor() -> str | None:
    """Return 'cloud', 'server', or None if neither responded with 200."""
    # Cloud: /rest/api/3/myself, HTTP Basic (email + API token).
    cloud_url = f"{JIRA_BASE_URL}/rest/api/3/myself"
    print("\n=== Detecting flavor ===")
    print(f"  Hint from XRAY_IS_CLOUD: {'cloud' if XRAY_IS_CLOUD_HINT else 'server/dc'}")
    print(f"  Trying Cloud: GET {cloud_url} (Basic auth)")
    try:
        resp = HTTP.get(cloud_url, auth=(JIRA_EMAIL, JIRA_TOKEN), timeout=15.0)
        if resp.status_code == 200:
            who = resp.json().get("displayName") or resp.json().get("emailAddress") or "?"
            print(f"  [ok] Cloud authenticated as: {who}")
            return "cloud"
        else:
            print(f"  [fail] Cloud returned HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [fail] Cloud request failed: {e}")

    # Server/DC: /rest/api/2/myself, Bearer (PAT) first, then Basic fallback.
    server_url = f"{JIRA_BASE_URL}/rest/api/2/myself"
    print(f"  Trying Server/DC: GET {server_url} (Bearer PAT)")
    try:
        resp = HTTP.get(
            server_url,
            headers={"Authorization": f"Bearer {JIRA_TOKEN}"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            who = resp.json().get("displayName") or resp.json().get("name") or "?"
            print(f"  [ok] Server/DC authenticated as: {who} (Bearer)")
            return "server"
        print(f"  [fail] Server/DC Bearer returned HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [fail] Server/DC Bearer request failed: {e}")

    print(f"  Trying Server/DC: GET {server_url} (Basic auth)")
    try:
        resp = HTTP.get(server_url, auth=(JIRA_EMAIL, JIRA_TOKEN), timeout=15.0)
        if resp.status_code == 200:
            who = resp.json().get("displayName") or resp.json().get("name") or "?"
            print(f"  [ok] Server/DC authenticated as: {who} (Basic)")
            return "server"
        print(f"  [fail] Server/DC Basic returned HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [fail] Server/DC Basic request failed: {e}")

    return None


STEPS_NAME_PATTERN = re.compile(r"test ?steps?|manual test steps", re.IGNORECASE)


def find_steps_field(flavor: str, issue_key: str) -> tuple[str, str] | None:
    """Return (field_id, field_name) for the Xray steps custom field, or None."""
    api_version = "3" if flavor == "cloud" else "2"
    url = f"{JIRA_BASE_URL}/rest/api/{api_version}/issue/{issue_key}"
    params = {"expand": "names,renderedFields,schema"}
    print(f"\n=== Inspecting {issue_key} ===")
    print(f"  GET {url}?expand=names,renderedFields,schema")
    try:
        if flavor == "cloud":
            resp = HTTP.get(
                url, params=params, auth=(JIRA_EMAIL, JIRA_TOKEN), timeout=30.0
            )
        else:
            resp = HTTP.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {JIRA_TOKEN}"},
                timeout=30.0,
            )
    except Exception as e:
        print(f"  [fail] Request failed: {e}")
        return None

    if resp.status_code != 200:
        print(f"  [fail] HTTP {resp.status_code}: {resp.text[:300]}")
        return None

    body = resp.json()
    names: dict[str, str] = body.get("names") or {}
    if not names:
        print("  [fail] Response had no 'names' map. Cannot map field IDs to names.")
        return None

    # First pass: a custom field whose human name looks like "test steps".
    for field_id, field_name in names.items():
        if field_id.startswith("customfield_") and STEPS_NAME_PATTERN.search(field_name):
            print(f"  [ok] Steps field: {field_id} (\"{field_name}\")")
            return field_id, field_name

    # Fallback: list all custom fields so the user can eyeball them.
    print("  [fail] No customfield_* matches /test ?steps?/i.")
    print("         All custom fields on this issue:")
    for field_id, field_name in sorted(names.items()):
        if field_id.startswith("customfield_"):
            print(f"           {field_id}  =  {field_name}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    parser.add_argument(
        "--issue-key",
        help="A real Jira test-case key (e.g. QA-1234) used to inspect the Xray steps field.",
    )
    args = parser.parse_args()

    print(f"mTLS: {_mtls.describe()}")

    flavor = detect_flavor()
    if flavor is None:
        print(
            "\n[fail] Could not authenticate to Jira via Cloud or Server/DC patterns.\n"
            "Check JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN, and whether your token has read access."
        )
        return 1

    flavor_label = "Cloud" if flavor == "cloud" else "Server/DC"
    if flavor == "cloud" and not XRAY_IS_CLOUD_HINT:
        print("  NOTE: XRAY_IS_CLOUD=false in .env but Cloud authenticated. Update .env.")
    if flavor == "server" and XRAY_IS_CLOUD_HINT:
        print("  NOTE: XRAY_IS_CLOUD=true in .env but Server/DC authenticated. Update .env.")

    if not args.issue_key:
        print(
            f"\n=== Summary ===\n  Flavor: {flavor_label}\n"
            "  Steps custom field ID: (skipped — pass --issue-key <KEY> to identify it)"
        )
        return 0

    found = find_steps_field(flavor, args.issue_key)
    print("\n=== Summary ===")
    print(f"  Flavor: {flavor_label}")
    if found:
        field_id, field_name = found
        print(f'  Steps custom field ID: {field_id}  (named "{field_name}")')
        return 0
    print("  Steps custom field ID: NOT FOUND (see custom-field listing above)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
