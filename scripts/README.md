# scripts/ — Phase 0 verification scripts

These scripts confirm the corporate LLM gateway, Jira/Xray, GitLab, and staging app are reachable and behave correctly before any pipeline code is written. They **must run on the company laptop** — the private PC used to author them cannot reach any of these services.

## Order

```
1. cp .env.example .env       # fill in values per section header
2. uv sync                    # install Phase 0 deps
3. uv run python scripts/step0_verify_tool_calling.py
4. uv run python scripts/step0b_verify_embeddings.py
5. uv run python scripts/step0c_xray_flavor.py --issue-key <real-QA-key>
```

Step 0 is the most important — if any candidate model fails tool calling, stop and fix the gateway before continuing.

## Environment variables per script

| Variable | step0 (tool calling) | step0b (embed/rerank) | step0c (xray flavor) |
|---|:-:|:-:|:-:|
| `LLM_BASE_URL` | required | required | — |
| `LLM_API_KEY` | required | required | — |
| `EMBEDDING_MODEL` | — | required (default ok) | — |
| `RERANKER_MODEL` | — | required (default ok) | — |
| `RERANK_ENDPOINT` | — | optional | — |
| `JIRA_BASE_URL` | — | — | required |
| `JIRA_EMAIL` | — | — | required |
| `JIRA_TOKEN` | — | — | required |
| `XRAY_IS_CLOUD` | — | — | optional (auto-detect) |
| `USE_HTTP_PROXY` | optional | optional | optional |

All scripts connect **directly by default** and ignore the environment's `HTTP(S)_PROXY` / `NO_PROXY` (the gateway is reached over the VPN; this does not bypass the VPN). Set `USE_HTTP_PROXY=true` only if your gateway/Jira is reachable solely through a proxy.

## Expected output

### step0_verify_tool_calling.py

```
=== Testing openai/gpt-oss-120b ===
  [ok] Tool called: get_weather
  [ok] Arguments:   {"city": "Zagreb"}
... (same for other two models)

=== Summary ===
  [ok]   openai/gpt-oss-120b
  [ok]   mistralai/devstral-small-2-2512
  [ok]   meta-llama/llama-3.3-70b-instruct

All candidate models pass tool calling. Safe to proceed.
```

Non-zero exit ⇒ at least one model failed.

### step0b_verify_embeddings.py

```
=== /embeddings (mxbai-embed-large) ===
  POST https://gw/v1/embeddings
  [ok] Returned vector of length 1024

=== /rerank (bge-reranker-v2-m3) ===
  POST https://gw/v1/rerank
  [ok] Returned 2 ranked results
        #0: index=0 score=0.91
        #1: index=1 score=0.12

=== Summary ===
  [ok]   /embeddings (mxbai-embed-large)
  [ok]   /rerank     (bge-reranker-v2-m3)
```

Vector length depends on the model (mxbai-embed-large is 1024).

### step0c_xray_flavor.py

```
=== Detecting flavor ===
  Hint from XRAY_IS_CLOUD: cloud
  Trying Cloud: GET https://yourcompany.atlassian.net/rest/api/3/myself (Basic auth)
  [ok] Cloud authenticated as: Jana Pucic

=== Inspecting QA-1234 ===
  GET .../rest/api/3/issue/QA-1234?expand=names,renderedFields,schema
  [ok] Steps field: customfield_10100 ("Manual Test Steps")

=== Summary ===
  Flavor: Cloud
  Steps custom field ID: customfield_10100  (named "Manual Test Steps")
```

If `--issue-key` is omitted, the steps-field check is skipped and only flavor is printed.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Any script: TLS/mTLS handshake OK, then "server disconnected without sending a response" | Routing through the environment-configured proxy drops the request — a direct `curl` works, the same `curl` via the proxy fails identically | Scripts ignore the env proxy **by default**; just don't set `USE_HTTP_PROXY` (and connect over the VPN). If the endpoint is only reachable through a proxy, set `USE_HTTP_PROXY=true`. If it still drops when direct, the gateway may fingerprint Python's TLS ClientHello — fall back to a libcurl-backed client (`curl_cffi`) |
| step0: "Model did not call any tool" | Gateway not forwarding `tools` param | Ask platform team; some gateways need `X-Use-Tools: true` header |
| step0: HTTP 404 with model name in error | Wrong model name | `GET /v1/models` to list available |
| step0b: HTTP 404 on `/rerank` | Rerank lives outside `/v1` | Set `RERANK_ENDPOINT=https://gw/rerank` in `.env` |
| step0b: empty vector returned | Wrong model name or model not loaded | Confirm with platform team |
| step0c: both flavors return 401 | Token doesn't have read access | Regenerate with proper scopes |
| step0c: "No customfield_* matches" | Steps field has a non-standard name | Eyeball the printed list, set field ID manually in Phase 1.A config |

The same gotchas are documented in [`AI_TEST_GENERATION_GUIDE.md`](../AI_TEST_GENERATION_GUIDE.md) §3.2.
