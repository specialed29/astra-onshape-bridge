# Astra Onshape Bridge

Browser chat → OpenAI Responses API (`gpt-6-astra`) → authenticated Streamable HTTP MCP → Onshape REST API.

## Current safety boundary

This release is a **read-only commissioning baseline**. The browser imports only six read-only tools. The three mutation/export tools remain discoverable for protocol compatibility, but reject execution unless `ENABLE_CAD_WRITES=true`. Leave it false during commissioning. A prompt is not an authorization boundary.

The existing raw-feature tool is not a complete CAD authoring system: there is no typed sketch/extrusion vocabulary, feature-update tool, geometry validation, approval UI, or complete STEP download flow yet. Do not interpret successful tool discovery as validation of CAD generation or export.

## Deployment

Use the included Render Blueprint. Python 3.14.3 and direct dependency versions are pinned to the tested runtime. The Blueprint build runs dependency checks and offline tests:

```sh
pip install -r requirements.txt
pip check
python -m unittest discover -s tests -v
```

Start command:

```sh
uvicorn app:app --host 0.0.0.0 --port "$PORT"
```

Use `/health` as the Render health-check path. It is a liveness/configuration-presence check, **not proof that external credentials work**. Check `commit` against the intended GitHub revision after every deploy. Existing services may require a Blueprint sync for build-command changes.

### Environment

| Name | Purpose |
|---|---|
| `OPENAI_API_KEY` | Required server-side OpenAI credential |
| `ONSHAPE_ACCESS_KEY` | Required server-side Onshape access key |
| `ONSHAPE_SECRET_KEY` | Required server-side Onshape secret key |
| `OPENAI_MODEL` | Defaults to `gpt-6-astra`; access must be verified with the configured OpenAI account |
| `ONSHAPE_BASE_URL` | Defaults to `https://cad.onshape.com`; must match the stack that issued the key |
| `PUBLIC_BASE_URL` | Canonical HTTPS origin, without `/mcp`; falls back only to `RENDER_EXTERNAL_URL`, never inbound Host headers |
| `MCP_BEARER_TOKEN` | High-entropy MCP token; Blueprint generates one. If omitted, derived server-side from the Onshape secret |
| `CHAT_BEARER_TOKEN` | Separate high-entropy browser-chat token; Blueprint generates one. Falls back to the MCP token if absent |
| `ENABLE_CAD_WRITES` | Defaults to `false`; guards all non-GET Onshape requests, including export jobs |
| `ONSHAPE_AUTH_MODE` | Defaults to `hmac`; `basic` is available for explicit diagnostic comparison |
| `PYTHON_VERSION` | Pinned to `3.14.3` on Render |

Never commit `.env`, put provider API keys in the browser, or copy tokens into logs. Obtain the chat token privately from your Render Environment settings and enter it in the UI's password field. It remains in page memory and is not saved to browser storage.

### Routes

| Route | Access / purpose |
|---|---|
| `GET /` | Public UI shell; no embedded credentials |
| `GET /health` | Public liveness, versions, commit, presence-only environment booleans |
| `GET /mcp-info` | Public transport metadata; no credentials |
| `POST /mcp` | FastMCP-native bearer authentication; JSON-RPC Streamable HTTP |
| `POST /api/chat` | Chat bearer token; read-only Responses API calls |
| `GET /api/diagnostics/onshape` | Chat bearer token; direct `GET /api/v10/documents?limit=1&offset=0`, bypassing MCP and OpenAI; returns count, not document contents |

The diagnostic endpoint accepts `?auth_mode=hmac` or `?auth_mode=basic` without changing service configuration.

## Commissioning

Run from a trusted shell with the service environment loaded:

```sh
python scripts/probe_readonly.py \
  --base-url https://your-service.onrender.com \
  --openai --onshape --chat
```

This checks, in order:

1. Public routes.
2. Authenticated `initialize`.
3. `notifications/initialized`.
4. `tools/list`, verifying all nine expected names.
5. A fresh OpenAI Responses request and its actual `mcp_list_tools` output item. Approval is required for every tool call in this discovery probe, so no tool can execute.
6. A direct Onshape document-list GET, outside MCP/OpenAI.
7. A read-only `/api/chat` request and completed `search_documents` trace.

The probe never starts a CAD write or export. It redacts known secrets and withholds document names/IDs from successful output. Do not turn on HTTP wire/debug logging.

For the browser, enter the chat token and send:

> List my Onshape documents. Do not modify anything.

### MCP wire contract

Use the exact URL `/mcp`, not `/mcp/`. Send:

```http
Authorization: Bearer <MCP_BEARER_TOKEN>
Content-Type: application/json
Accept: application/json, text/event-stream
```

Initialize:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"manual-probe","version":"1.0"}}}
```

Then send the notification (no ID):

```json
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

Then list tools:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

Send the negotiated `MCP-Protocol-Version` on subsequent requests. This app is stateless and normally returns no `Mcp-Session-Id`. The probe preserves one if a server does return it. JSON responses are enabled to avoid unnecessary SSE buffering. `/mcp/` redirects to `/mcp`; don't rely on redirect-preserved Authorization headers.

In the Responses tool configuration, `authorization` is the **raw token**, without the `Bearer ` prefix. Include it on every Responses request. See [OpenAI remote MCP documentation](https://developers.openai.com/api/docs/guides/tools-remote-mcp).

### Tools

Read-only: `onshape_health`, `search_documents`, `get_document`, `list_elements`, `get_partstudio_features`, `get_translation`.

Guarded mutations/export: `create_document`, `add_feature_raw`, `export_partstudio_step`.

Document search supports `offset` and `limit`. Do not mistake one returned page for the entire account.

## Failure isolation

| Symptom | Check |
|---|---|
| Installation fails | FastMCP 4 requires Starlette 1.x. The former `starlette<1` constraint was incompatible |
| `/health` lacks version/commit fields | Old build is still running; compare Render's live deploy SHA with GitHub |
| `/mcp-info` returns 401 | Old `startswith("/mcp")` middleware may still be deployed; this route is intentionally public now |
| MCP 401 | Raw token versus `Bearer` header formatting, matching server/client token, whitespace, stale deployment |
| MCP 307/308 | Use the exact canonical `/mcp` endpoint without a trailing slash |
| MCP 406 | Use a compatible Accept header, preferably both JSON and event-stream |
| OpenAI 424 | First run direct MCP initialization/listing. Check canonical URL, deployment, auth, and server availability while chat is pending |
| Chat self-callback times out | Never use synchronous `OpenAI.responses.create()` inside the async route. It blocks the worker that must answer OpenAI's callback. This implementation awaits `AsyncOpenAI` |
| Onshape 401 | Key pair, issuing stack, HMAC canonicalization, UTC clock within five minutes |
| Onshape 403 | API-key read scope, account and document permissions |
| Onshape 429 | Rate limit; retry later without blindly retrying mutations |

HMAC signs the exact encoded pathname and query, includes a trailing newline, uses a fresh nonce and HTTP Date, and lowercases the canonical string. Basic API-key auth is supported by Onshape for local testing, so a failure must not be attributed to Basic auth merely because it is Basic. See [Onshape API-key authentication](https://onshape-public.github.io/docs/auth/apikeys/).

Redirects are not followed automatically: a signed redirect needs a newly validated target and a fresh signature. This especially matters for future file downloads.

## STEP and parametric CAD follow-up

The existing format-specific STEP export endpoint is valid in the published API: `POST /partstudios/d/{did}/{wv}/{wvid}/e/{eid}/export/step`. It initiates asynchronous translation, not a file download. See [Onshape import/export documentation](https://onshape-public.github.io/docs/api-adv/translation/).

After explicit authorization for a disposable test document, the next commissioning phase should:

1. Add typed feature creation/update with explicit units, IDs, and feature-state validation.
2. Require review of each write's target and complete payload.
3. Create and edit a small parametric test part, preserving editability in Onshape.
4. Start STEP export with `storeInDocument=false` unless a new blob tab is explicitly wanted.
5. Poll translation to `DONE`, handle `FAILED`, retrieve the resulting external data or blob, safely re-sign redirects, and stream bytes instead of truncating text into a tool response.
6. Validate the STEP payload and provide an authenticated download.

Do not run this phase during the read-only test.
