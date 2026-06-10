# scripts/ — access-verification scripts

These scripts confirm your LLM gateway, Jira/Xray, and embedding endpoints are reachable and behave correctly before you run the pipeline. Run them from a machine that can reach those services.

## Order

```
1. cp .env.example .env       # fill in values per section header
2. uv sync                    # install deps
3. uv run python scripts/step0_verify_tool_calling.py
4. uv run python scripts/step0b_verify_embeddings.py
5. uv run python scripts/step0c_xray_flavor.py --issue-key <real-QA-key>
6. uv run python scripts/step0d_verify_reasoning_effort.py   # only if you plan to set *_REASONING_EFFORT
```

Step 0 is the most important — if any candidate model fails tool calling, stop and fix the gateway before continuing.

Step 0d is optional: it proves whether the gateway honors the `reasoning_effort` request param
(many gateways silently drop unknown params). Set `PLANNER_REASONING_EFFORT` /
`HEALER_REASONING_EFFORT` in `.env` only after it reports **HONORED**.

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

All scripts connect **directly by default** and ignore the environment's `HTTP(S)_PROXY` / `NO_PROXY`. Set `USE_HTTP_PROXY=true` only if your gateway/Jira is reachable solely through a proxy.

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
  Hint from XRAY_IS_CLOUD: server/dc
  Trying Server/DC: GET https://your-jira/rest/api/2/myself (Bearer PAT)
  [ok] Server/DC authenticated as: QA Bot

=== Inspecting QA-1234 ===
  GET .../rest/api/2/issue/QA-1234?expand=names,renderedFields,schema
  [ok] Steps field: customfield_11006 ("Manual Test Steps")

=== Summary ===
  Flavor: Server/DC
  Steps custom field ID: customfield_11006  (named "Manual Test Steps")
```

If `--issue-key` is omitted, the steps-field check is skipped and only flavor is printed.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Any script: TLS/mTLS handshake OK, then "server disconnected without sending a response" | Routing through the environment-configured proxy drops the request — a direct `curl` works, the same `curl` via the proxy fails identically | Scripts ignore the env proxy **by default**; just don't set `USE_HTTP_PROXY`. If the endpoint is only reachable through a proxy, set `USE_HTTP_PROXY=true`. If it still drops when direct, the gateway may fingerprint Python's TLS ClientHello — fall back to a libcurl-backed client (`curl_cffi`) |
| step0: "Model did not call any tool" | Gateway not forwarding `tools` param | Ask platform team; some gateways need `X-Use-Tools: true` header |
| step0: HTTP 404 with model name in error | Wrong model name | `GET /v1/models` to list available |
| step0b: HTTP 404 on `/rerank` | Rerank lives outside `/v1` | Set `RERANK_ENDPOINT=https://gw/rerank` in `.env` |
| step0b: empty vector returned | Wrong model name or model not loaded | Confirm with platform team |
| step0c: both flavors return 401 | Token doesn't have read access | Regenerate with proper scopes |
| step0c: "No customfield_* matches" | Steps field has a non-standard name | Eyeball the printed list, then set `XRAY_STEPS_FIELD_ID` in `.env` for the Xray client |

The same gotchas are documented in [`AI_TEST_GENERATION_GUIDE.md`](../AI_TEST_GENERATION_GUIDE.md) §3.2.

---

## Auth-state scripts (legacy, optional)

> **Legacy / optional.** The pipeline uses **context-driven login** — each agent and each generated test logs in live from the `project_context.md` dummy creds, so there is no saved `storage_state.json` in the runtime path. These two scripts are kept only as a manual session-capture utility (e.g. to debug a login flow). They drive the live staging app.

| Script | What it does |
|---|---|
| `save_auth_state.py` | Opens a headed Chromium, logs into staging with `STAGING_USERNAME` / `STAGING_PASSWORD`, and writes `output/storage_state.json`. |
| `verify_auth_state.py` | Loads that file, hits a protected route, and exits non-zero if it's bounced back to the login page. Run it right after `save_auth_state.py`. |

```bash
uv run playwright install chromium          # one-time: Chromium binary for the Python playwright pkg
uv run python scripts/save_auth_state.py
uv run python scripts/verify_auth_state.py
```

If you do use them, adjust the Keycloak selectors (nav login `#metaMenuItem5` → `#username` / `#password` / submit `#kc-login`) to the real staging form, and record the auth flow in [`project_map.md`](../project_map.md). `output/storage_state.json` is gitignored. The pipeline itself does not read this file.
