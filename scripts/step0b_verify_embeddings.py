"""
Step 0b — verify the internal LLM gateway exposes /embeddings and a rerank endpoint.

RAG (Phase 2.5) needs both. Confirming today saves a multi-week surprise later.

Reads from .env:
  LLM_BASE_URL                — OpenAI-compatible base, e.g. https://gw/v1
  LLM_API_KEY                 — gateway secret
  EMBEDDING_MODEL             — e.g. mxbai-embed-large
  RERANKER_MODEL              — e.g. bge-reranker-v2-m3
  RERANK_ENDPOINT             — optional: full URL override if /rerank is elsewhere
  MTLS_PKCS12_FILE/PASSWORD   — optional: mTLS client cert as a .pfx/.p12 bundle
  MTLS_CERT_FILE / KEY_FILE   — optional: same as above but separate PEM files
  SSL_CERT_FILE               — optional: corporate root CA bundle
  USE_HTTP_PROXY              — optional: "true" honors env HTTP(S)_PROXY;
                                default is DIRECT (env HTTP(S)_PROXY ignored)

Must run on the company laptop.

Run:
  uv run python scripts/step0b_verify_embeddings.py
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _mtls  # noqa: E402

if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_API_KEY"):
    print("[fail] LLM_BASE_URL and LLM_API_KEY must be set in .env")
    sys.exit(2)

LLM_BASE_URL = os.environ["LLM_BASE_URL"]
LLM_API_KEY = os.environ["LLM_API_KEY"]
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "mxbai-embed-large")
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "bge-reranker-v2-m3")
RERANK_ENDPOINT = os.environ.get("RERANK_ENDPOINT") or None

try:
    _cert = _mtls.get_cert_arg()
except Exception as e:
    print(f"[fail] mTLS setup failed: {type(e).__name__}: {e}")
    sys.exit(2)

# Default is direct — the env HTTP(S)_PROXY is ignored unless USE_HTTP_PROXY=true
# (trust_env). The CA bundle (verify) is always applied. See _mtls.get_trust_env.
_client_kwargs: dict = {
    "timeout": 30.0,
    "trust_env": _mtls.get_trust_env(),
    "verify": _mtls.get_verify_arg(),
}
if _cert is not None:
    _client_kwargs["cert"] = _cert
HTTP = httpx.Client(**_client_kwargs)

HEADERS = {
    "Authorization": f"Bearer {LLM_API_KEY}",
    "Content-Type": "application/json",
}


def test_embeddings() -> bool:
    url = f"{LLM_BASE_URL.rstrip('/')}/embeddings"
    print(f"\n=== /embeddings ({EMBEDDING_MODEL}) ===")
    print(f"  POST {url}")
    try:
        resp = HTTP.post(
            url,
            headers=HEADERS,
            json={"model": EMBEDDING_MODEL, "input": ["smoke test"]},
        )
    except Exception as e:
        print(f"  [fail] Request failed: {e}")
        return False

    if resp.status_code != 200:
        print(f"  [fail] HTTP {resp.status_code}: {resp.text[:300]}")
        return False

    try:
        body = resp.json()
        vec = body["data"][0]["embedding"]
    except (KeyError, IndexError, ValueError) as e:
        print(f"  [fail] Unexpected response shape: {e}")
        print(f"         Body: {resp.text[:300]}")
        return False

    if not isinstance(vec, list) or len(vec) == 0:
        print(f"  [fail] Embedding vector is empty or not a list: {type(vec).__name__}")
        return False

    print(f"  [ok] Returned vector of length {len(vec)}")
    return True


def test_rerank() -> bool:
    url = RERANK_ENDPOINT or f"{LLM_BASE_URL.rstrip('/')}/rerank"
    print(f"\n=== /rerank ({RERANKER_MODEL}) ===")
    print(f"  POST {url}")
    payload = {
        "model": RERANKER_MODEL,
        "query": "user login flow",
        "documents": [
            "The user enters credentials and signs in.",
            "Customer adds an item to the shopping cart.",
        ],
    }
    try:
        resp = HTTP.post(url, headers=HEADERS, json=payload)
    except Exception as e:
        print(f"  [fail] Request failed: {e}")
        return False

    if resp.status_code != 200:
        print(f"  [fail] HTTP {resp.status_code}: {resp.text[:300]}")
        return False

    try:
        body = resp.json()
    except ValueError as e:
        print(f"  [fail] Response is not valid JSON: {e}")
        return False

    # Different gateways return different shapes. Accept any of:
    #   {"results": [{"index": int, "relevance_score": float}, ...]}    (Cohere-style)
    #   {"data":    [{"index": int, "score":           float}, ...]}    (TEI-style)
    #   [{"index": int, "score": float}, ...]                            (bare list)
    candidates = None
    if isinstance(body, dict):
        candidates = body.get("results") or body.get("data")
    elif isinstance(body, list):
        candidates = body

    if not candidates or not isinstance(candidates, list) or len(candidates) == 0:
        print(f"  [fail] No ranked results in response. Body: {str(body)[:300]}")
        return False

    print(f"  [ok] Returned {len(candidates)} ranked results")
    for i, item in enumerate(candidates[:3]):
        idx = item.get("index") if isinstance(item, dict) else None
        score = None
        if isinstance(item, dict):
            score = item.get("relevance_score") or item.get("score")
        print(f"        #{i}: index={idx} score={score}")
    return True


def main() -> int:
    print(f"mTLS: {_mtls.describe()}")
    print(f"Proxy: {_mtls.describe_trust_env()}")
    emb_ok = test_embeddings()
    rerank_ok = test_rerank()
    print("\n=== Summary ===")
    print(f"  {'[ok]  ' if emb_ok else '[fail]'} /embeddings ({EMBEDDING_MODEL})")
    print(f"  {'[ok]  ' if rerank_ok else '[fail]'} /rerank     ({RERANKER_MODEL})")
    if not (emb_ok and rerank_ok):
        print(
            "\nWARNING: embeddings or rerank endpoint did not respond as expected.\n"
            "Common causes:\n"
            "  - gateway exposes a different path (try /v1/embeddings vs /embeddings, "
            "or set RERANK_ENDPOINT)\n"
            "  - model name not deployed on this gateway "
            "(GET /v1/models to list)\n"
            "  - rerank uses a non-OpenAI response shape — adjust the parser above\n"
            "  - mxbai-embed-large default is German-tuned (-de-); ask platform team "
            "for English/multilingual variant"
        )
        return 1
    print("\nBoth endpoints respond. RAG (Phase 2.5) will be unblocked when the time comes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
