# Astra Onshape Bridge

**Paused by owner request on October 7, 2026.** Production is gated by `BRIDGE_OFFLINE=true`, which returns 503 without dispatching chat, MCP, downloads, or the browser UI. `/health` returns 200 with `app=false, offline=true` solely for Render deployment health. This is application-level shutdown, not Render service suspension. No development or reactivation should occur without a new owner request; source, credentials, and Onshape documents are preserved.

Browser chat → OpenAI Responses API (`gpt-6-astra`) → authenticated Streamable HTTP MCP → Onshape REST API.

## Typed modeling release

The bridge now includes native rectangle/circle sketches, NEW solid extrusions, dimensional edits, and geometry inspection. This is the initial modeling toolset, not a general-purpose feature authoring system. Live creation/edit commissioning must be performed on a specifically approved disposable document before assuming all payloads work for a production part.

In browser chat, each mutation produces an OpenAI MCP approval request. The UI shows the server, tool, exact arguments with defaults, target IDs, dimensions, and document visibility, then pauses for **Approve** or **Deny**. No approval is inferred from the model's prose. A server-side, one-use review token binds the decision to the original response and arguments; the browser cannot substitute different arguments or a different response ID.

Review tokens expire after 30 minutes or a service restart. Do not run multiple Uvicorn workers or multiple Render instances with this in-memory approval store; migrate it to a shared, durable store before scaling. On timeout, a mutation's outcome can be unknown: inspect Onshape before requesting a replacement action, and never blindly retry.

### Current tools

| Tool | Scope |
|---|---|
| `create_document` | New document; `is_public=false` by default, with no silent fallback to public |
| `create_partstudio` | New Part Studio tab in an explicit document/workspace |
| `create_rectangle_sketch` | Width/height dimensions in mm; fixed lower-left vertex at `x_mm,y_mm`; default plane |
| `create_circle_sketch` | Diameter in mm; fixed center at `x_mm,y_mm`; default plane |
| `extrude_sketch` | All closed regions of a specified sketch into NEW solids, with explicit depth in mm |
| `set_feature_dimension` | Bridge sketch width/height/diameter or NEW/BLIND extrusion depth; retains feature ID |
| `inspect_partstudio_geometry` | Read-only actual parts, bounding boxes, and mass properties |
| `export_partstudio_step` | Review-controlled STEP export of all parts in the selected Part Studio; external file only |

The other six read-only tools remain available. `add_feature_raw` remains outside the browser toolset; raw writes require a second independent `ENABLE_RAW_FEATURE_WRITES=true` operator setting and are disabled by default. Cut/add/intersect operations, holes, fillets, arbitrary drawings, assemblies, and materials are not exposed by this modeling release.

New feature writes read the current `sourceMicroversion` and use `rejectMicroversionSkew=true` to reject intervening changes. HTTP success is not geometric success: tools report `ok=true` only when the returned feature state is `OK`, and the assistant is instructed to inspect real geometry afterward. A failed feature can still exist in the feature tree; it is never silently deleted or re-created.

Refresh the browser and start **New conversation** after deployment so earlier read-only MCP tool-list context is not reused. Example request: “Create a private document named My First Plate, sketch a 40 × 30 mm rectangle on Top, and extrude it 8 mm. Ask me to approve each write.” This is an example, not authorization to execute it.

## Server safety switches

With `ENABLE_CAD_WRITES=false`, the browser is read-only and all Onshape non-GET requests are blocked. With `ENABLE_CAD_WRITES=true`, browser chat imports six typed modeling mutations plus STEP export and requires approval for every one; read tools do not require approval. Previously completed files can still be downloaded with chat authentication when writes are disabled. A prompt is not an authorization boundary.

The MCP bearer token is an operator credential: a separate trusted MCP client using it directly can invoke enabled mutations without going through this browser's approval UI. Do not give that token to untrusted clients or embed it in the browser. The browser uses only the separate chat token, while OpenAI applies the configured MCP approval policy.

## Deployment

Use the included Render Blueprint. Python 3.14.3 and direct dependency versions are pinned to the tested runtime. The Blueprint build runs dependency checks and offline tests:

The modeling Blueprint enables `ENABLE_CAD_WRITES=true` and keeps `ENABLE_RAW_FEATURE_WRITES=false`; browser approvals remain mandatory. Set the former to false if you want a read-only deployment.

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
| `ENABLE_RAW_FEATURE_WRITES` | Defaults to `false`; independent guard on raw feature writes; never exposed in browser chat |
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
| `POST /api/chat` | Chat bearer token; reads and typed modeling, with required MCP approval for each mutation |
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
4. `tools/list`, verifying the current read/modeling/legacy tool definitions.
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

Read-only: `onshape_health`, `search_documents`, `get_document`, `list_elements`, `get_partstudio_features`, `get_translation`, `inspect_partstudio_geometry`.

Typed mutations and STEP export are listed above. Legacy operator-only tool: `add_feature_raw`.

Document search supports `offset` and `limit`, with page size capped at the verified 20 items. Do not mistake one returned page for the entire account; request subsequent pages with offsets 20, 40, and so on.

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

JSON requests never follow redirects automatically. File download redirects are bounded and checked separately as described below.

## STEP export and authenticated download

The existing format-specific STEP export endpoint is valid in the published API: `POST /partstudios/d/{did}/{wv}/{wvid}/e/{eid}/export/step`. It initiates asynchronous translation, not a file download. See [Onshape import/export documentation](https://onshape-public.github.io/docs/api-adv/translation/).

After refreshing chat and starting a new conversation, ask: “Export this Part Studio as STEP without changing geometry or adding tabs,” and provide the exact Onshape URL. Review the export target and `store_in_document=false` before approving.

1. `export_partstudio_step` creates one external STEP translation and requests `stepUnit=MILLIMETER`. The commissioned Onshape result nevertheless declared metres in its STEP metadata; an independent reader correctly resolved it to 40 × 30 × 12 mm. Respect the file's declared units rather than assuming its numeric coordinates are millimetres. `store_in_document=true` is rejected; no blob tab is created.
2. `get_translation` returns `ACTIVE`, `DONE`, or `FAILED`. The model polls at most three times in a response; an active job can be checked again by ID without creating another job.
3. A completed translation returns download descriptors. Chat renders **Download STEP** buttons that fetch `GET /api/exports/{translation_id}/{file_index}` with the chat bearer in a header, never a URL.
4. The route rechecks completion, resolves the external data ID server-side, reads binary chunks with a 32 MiB limit, and checks the STEP Part 21 envelope. If Onshape wraps the result in ZIP, exactly one STEP member is read in memory, with a separate 32 MiB decompressed limit and CRC checking; no archive path is extracted to disk. Multi-file and encrypted archives are rejected. It returns a plain STEP attachment with SHA-256, `Cache-Control: no-store`, and `nosniff`. Raw CAD data never enters model tool output.
5. Redirects are limited to four requests. Only the configured Onshape origin receives freshly signed HMAC or Basic auth. Presigned `*.amazonaws.com` storage is fetched without Onshape credentials; all other cross-origin redirects fail closed.

The server's envelope check is not a geometric-kernel validation. Commission exported sample files using an independent STEP reader, checking topology, bounds, volume, and units. Files are not persisted by the bridge; browser downloads are fetched on demand. Exports contain B-rep geometry, not the editable Onshape feature history.
