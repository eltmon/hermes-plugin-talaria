# PRD — Talaria (`hermes-plugin-t3code`, plugin id `t3code`)

Status: M1+M2 implemented · 2026-09-01
Companion: [design.md](design.md) (investigation detail), [`../spec.xbrief.json`](../spec.xbrief.json) (task plan).

This PRD is also the implementation brief. It is written so an implementation
agent can execute each work item without re-researching the two upstream
codebases. All file references were verified on 2026-08-31 against
`pingdotgg/t3code` (contracts v0.0.33) and `NousResearch/hermes-agent`
(main @ 3aee290899). Where a line number may drift, a searchable symbol is
given.

## Glossary

- **Hermes / hermes-agent** — Nous Research's open-source Python agent framework. Hosts this plugin.
- **Plugin** — a Hermes extension: a directory with `plugin.yaml` + `__init__.py` defining `register(ctx)`. Contributes tools, CLI commands, hooks. Opt-in via `plugins.enabled`.
- **Tool** — a function the hermes LLM can call. Handler contract: `(args: dict, **kwargs) -> str`, returns JSON, never raises.
- **T3 Code** — open-source GUI/server for coding agents (`pingdotgg/t3code`). A Node WebSocket server wraps provider CLIs.
- **Environment** — one running T3 Code server plus the machine, filesystem, and state it owns. Identified by `environmentId`.
- **Project / thread / turn** — T3 Code's workspace record / durable conversation / one user-to-agent cycle.
- **Dispatch** — T3 Code's single mutation door: a `ClientOrchestrationCommand` posted to `POST /api/orchestration/dispatch` (HTTP) or `orchestration.dispatchCommand` (WS).
- **Pairing** — T3 Code device onboarding: `t3 pair` prints a URL whose hash carries a one-time bootstrap credential, exchanged at `/oauth/token` for a 30-day bearer.
- **T3 Connect / relay** — T3 Code's tunnel + discovery service. The relay lists linked environments and brokers short-lived credentials; RPC traffic goes directly to each environment's Cloudflare tunnel.
- **DPoP** — RFC 9449 proof-of-possession: requests carry a JWT proof signed with a client ES256 key; tokens are bound to the key's thumbprint. Mandatory on the relay path only.
- **Mode A / Mode B** — the plugin's two connectivity modes: A = direct URL + pairing bearer; B = T3 Connect (Clerk OAuth + relay discovery + DPoP + tunnel).
- **xBRIEF** — the machine-readable task plan format used in `spec.xbrief.json`.

## Problem

Hermes and T3 Code run on different machines (reference deployment: hermes on
Fly.io, T3 Code on a laptop behind NAT, possibly several instances linked via
T3 Connect). There is no existing bridge: hermes cannot list T3 threads, send
prompts, answer approval requests, or touch the files T3 Code's agents work
on. T3 Code's own MCP server covers browser-preview automation only, and
hermes' MCP client speaks stdio/HTTP/SSE, not T3's WebSocket — so a native
hermes plugin is the right shape.

## Requirements

### Functional

- **FR-1** List configured/discovered environments with liveness and auth status.
- **FR-2** List projects and threads of an environment, including per-thread `worktreePath`, `latestTurn`, `hasPendingApprovals`, `hasPendingUserInput`.
- **FR-3** Read a thread's turn history and agent output.
- **FR-4** Create a thread; start a turn (send a prompt) with selectable model and runtime/interaction mode.
- **FR-5** Interrupt a turn; respond to approval requests and user-input requests.
- **FR-6** Wait for a turn to settle or block on approval, with bounded timeout (poll-based in M1).
- **FR-7** Mode A auth: consume a `t3 pair` URL via `hermes t3code login <url>`, exchange for a 30-day standard-scope bearer, store as a hermes secret, surface 401 as a "re-pair" error.
- **FR-8** Mode B auth: headless Clerk PKCE OAuth via `hermes t3code connect` (print URL, accept pasted code), then relay environment discovery.
- **FR-9** Mode B connect: DPoP token exchange with the relay, per-environment credential, environment-local DPoP access token with refresh on expiry.
- **FR-10** Remote file access: read, write, list, search files in an environment's projects (WS RPC; M2).
- **FR-11** Live events: subscribe to thread/shell streams and push salient events (turn finished, approval requested) into the hermes conversation (M2, gated on user grant).
- **FR-12** Multi-instance: every tool takes an optional `environment` argument; config supports many environments; mode B discovers them without per-instance config.

### Non-functional

- **NFR-1** Outbound-only networking from the hermes host. No listener, no inbound port.
- **NFR-2** Secrets (bearers, Clerk tokens, DPoP private key) live in hermes' secret surface, never in `plugin-data/`, `config.yaml`, or the install dir.
- **NFR-3** Tool handlers never raise and always return JSON strings (hermes contract).
- **NFR-4** No socket opened during `register()` (hermes `plugins doctor` blocks sockets at registration; connections are lazy).
- **NFR-5** Tolerate unknown fields in every T3 payload — T3 has no protocol-version header; compatibility is schema-side.
- **NFR-6** Distributable: installable via `hermes plugins install`, opt-in enable, `hermes plugins doctor . --ci` green in CI.

## Decisions (made here, not left to the executor)

1. **Native hermes plugin, Python, standalone repo.** Not MCP (transport mismatch), not a hermes-core patch (hermes placement policy: third-party integrations ship as standalone repos — `CONTRIBUTING.md:88-101` in hermes-agent).
2. **Plugin id `t3code`, toolset `t3code`, repo `hermes-plugin-talaria`.** Hermes installs key off the manifest `name:` (`hermes_cli/plugins_cmd.py`, `_install_plugin_core`: `plugin_name = manifest.get("name")`), so the repo name is free.
3. **HTTP-first.** All of FR-1..FR-6 use T3's typed HTTP API; the WS (Effect RPC) client is Milestone 2 and only needed for FR-10/FR-11.
4. **Two auth modes behind one environment-API layer.** The relay never proxies RPC; both modes end at the same environment handshake, so `t3_env.py` is mode-agnostic and auth modules only produce (base_url, auth headers/credential) pairs.
5. **Milestones:** M1 = HTTP core + mode A + mode B + tools FR-1..FR-6. M2 = WS client + FR-10/FR-11. M3 = distribution polish.
6. **Dependencies:** `httpx` (hermes core dep), `websockets` (hermes core pin, M2), `cryptography` (ES256 for DPoP; declared in the manifest, not auto-installed — hermes never auto-installs `python_dependencies`).
7. **Secret naming:** per-environment `T3CODE_TOKEN_<NAME>` (name uppercased, non-alphanumerics → `_`); mode B account-level `T3CODE_CLERK_REFRESH_TOKEN`, `T3CODE_CLERK_ACCESS_TOKEN`, `T3CODE_DPOP_KEY` (PEM, base64). Reads go through `agent.secret_scope.get_secret` with the `UnscopedSecretError` → `os.getenv` fallback (canonical pattern: hermes `plugins/platforms/homeassistant/adapter.py:43`).

## The T3 Code contract (transcribed — `@t3tools/contracts` is private, not on npm)

Verified endpoints (schemas in `packages/contracts/src/environmentHttp.ts`, orchestration types in `packages/contracts/src/orchestration.ts` of t3code):

| Call | Auth | Purpose |
|---|---|---|
| `GET /.well-known/t3/environment` | none | descriptor: `environmentId`, label, capabilities, sessionMethods |
| `POST /oauth/token` | none (carries subject token) | RFC 8693 exchange: pairing credential or relay credential → access token |
| `GET /api/orchestration/shell` | `orchestration:read` | all projects + threads (`OrchestrationShellSnapshot`) |
| `GET /api/orchestration/threads/:id?turnLimit=N&beforeCursor=…` | `orchestration:read` | thread detail + turn history |
| `POST /api/orchestration/dispatch` | `orchestration:operate` | every mutation (`ClientOrchestrationCommand`) |
| `POST /api/auth/websocket-ticket` | any valid token | 5-min ticket; connect `wss://…/ws?wsTicket=<ticket>` (M2) |

Pairing exchange body (form-encoded):

```
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token=<token from pairing URL hash>
subject_token_type=urn:t3:params:oauth:token-type:environment-bootstrap
requested_token_type=urn:ietf:params:oauth:token-type:access_token
scope=orchestration:read orchestration:operate terminal:operate review:write relay:read
client_label=hermes-talaria
```

Response: `{ access_token, token_type: "Bearer", expires_in: 2592000, scope }`.
The requested scope must be a subset of the bootstrap grant (server enforces).

`thread.turn.start` command shape (t3code `orchestration.ts`, `ClientThreadTurnStartCommand`):

```json
{
  "type": "thread.turn.start",
  "commandId": "<uuid4>",
  "threadId": "<thread id>",
  "message": { "messageId": "<uuid4>", "role": "user", "text": "<prompt>", "attachments": [] },
  "modelSelection": { "instanceId": "<provider instance slug>", "model": "<model id>" },
  "runtimeMode": "approval-required | auto-accept-edits | auto | full-access",
  "interactionMode": "default | plan",
  "createdAt": "<ISO 8601>"
}
```

`thread.create` requires `{ type, commandId, threadId (uuid4, client-generated), projectId, title, modelSelection, runtimeMode, interactionMode, branch: null, worktreePath: null, createdAt }`. Other commands used: `thread.turn.interrupt`, `thread.approval.respond`, `thread.user-input.respond`.

Mode B endpoints (relay contracts in t3code `packages/contracts/src/relay.ts`; client reference `packages/client-runtime/src/relay/managedRelay.ts`):

| Call | Auth | Purpose |
|---|---|---|
| `GET <relay>/v1/environments` | Clerk bearer | `{ environments: [{ environmentId, label, endpoint: { httpBaseUrl, wsBaseUrl }, linkedAt }] }` |
| `POST <relay>/v1/client/dpop-token` | Clerk bearer + `dpop` proof header | RFC 8693 → relay DPoP token, `scope=environment:connect environment:status` |
| `POST <relay>/v1/environments/:id/status` | relay DPoP token | liveness: `{ status: "online"|"offline", endpoint, descriptor? }` |
| `POST <relay>/v1/environments/:id/connect` | relay DPoP token + proof | `{ credential, endpoint, expiresAt }` — bootstrap credential bound to the client JWK thumbprint |

Then, at the environment (through the tunnel): exchange `credential` at
`POST /oauth/token` **with a DPoP proof header** → 1-hour DPoP-bound access
token; every subsequent request carries `Authorization: DPoP <token>` plus a
fresh `dpop` proof. Reference: t3code
`packages/client-runtime/src/authorization/remote.ts` (`exchangeRemoteDpopAccessToken`).

Clerk headless OAuth (mode B step 1) — mirrors `t3 connect login --headless`
(t3code `apps/server/src/cli/connect.ts` + `packages/shared/src/connectAuth.ts`):

1. Generate PKCE verifier/challenge (S256) and a random base64url `state`.
2. Print `https://app.t3.codes/connect#state=<state>&challenge=<challenge>` (hash params, `buildConnectAuthorizeRequestUrl`). No `port` param → out-of-band flow.
3. User opens it in any browser, signs in; the hosted callback page shows a blob `"<code>.<state>"` (`encodeConnectAuthCode`, separator `.`).
4. User pastes the blob; validate state matches (`checkConnectAuthCode` semantics), split off the code.
5. Exchange code at Clerk's frontend-API `/oauth/token` with `client_id=<T3CODE_CLERK_CLI_OAUTH_CLIENT_ID>`, `code_verifier`, `redirect_uri=https://app.t3.codes/connect/callback`. The frontend-API host is derived from the publishable key: strip `pk_live_`/`pk_test_`, base64-decode, drop the trailing `$` → hostname (e.g. `clerk.t3.codes`).
6. Store access + refresh tokens as hermes secrets; refresh on expiry.

Public identifiers (defaults; overridable in plugin settings — from t3code
`.env.example`, not secrets): publishable key `pk_live_Y2xlcmsudDMuY29kZXMk`,
CLI OAuth client id `hzxSgY2cH10sDU2r`, relay `https://relay.t3.codes`,
hosted app `https://app.t3.codes`.

Known traps (verified; do not re-derive):

- `WS_METHODS.projectsList/projectsAdd/projectsRemove` in t3code `rpc.ts` are dead constants — not registered RPCs. Listing goes through the orchestration shell.
- File ops (`projects.readFile/writeFile/searchContents/searchEntries/listEntries`) are WS-only; there is no HTTP file API.
- T3's MCP server (`POST /mcp`) is preview automation only.

## Hermes plugin contract (restated — binding on every work item)

- Plugins are **opt-in**: nothing loads until the user adds `t3code` to `plugins.enabled`. README must say so.
- `register(ctx)` runs once; **no network I/O inside it** (doctor blocks sockets). Clients are lazy singletons via `plugins/plugin_utils.py` `lazy_singleton` — never hand-rolled `global x` + `is None` (hermes is multi-threaded).
- Tools: `ctx.register_tool(name=…, toolset="t3code", schema=…, handler=…, check_fn=…, emoji=…)`. Schema is `{name, description, parameters:{type:"object",…}}`. Handlers `(args, **kwargs) -> str`, JSON out, errors as `{"error": …}` JSON, never raise.
- CLI: `ctx.register_cli_command` for `hermes t3code login|connect|environments|logout`.
- Settings via `ctx.get_config`/`set_config` (namespaced under `plugins.entries.t3code.settings`); runtime cursors via `ctx.state`; durable non-secret data via `plugins/plugin_storage.plugin_data_dir("t3code")`. Never write into the install dir.
- Secrets via `agent.secret_scope.get_secret` with unscoped fallback (see Decision 7).
- Hook callbacks and handlers accept `**kwargs` (forward compat).
- Cleanup via `ctx.on_unload`; M2 background readers via `ctx.spawn_task` (requires a running loop — start lazily from an async tool handler, never in `register()`).
- `ctx.inject_message` (M2) requires the user grant `plugins.entries.t3code.allow_gateway_injection: true` and, in gateway mode, a `session_key`.
- Manifest v2: `manifest_version: 2`, `kind: backend`, `config_schema`, `python_dependencies: ["cryptography"]` (declared/surfaced only — hermes never auto-installs).

## Configuration (target shape)

```yaml
plugins:
  enabled: [t3code]
  entries:
    t3code:
      settings:
        t3connect:
          enabled: true
          relay_url: https://relay.t3.codes
          hosted_app_url: https://app.t3.codes
          clerk_publishable_key: pk_live_Y2xlcmsudDMuY29kZXMk
          clerk_oauth_client_id: hzxSgY2cH10sDU2r
        environments:            # mode A entries; name → base_url
          laptop: { base_url: "https://t3.tail1234.ts.net" }
        default_environment: null
```

Environment resolution order for a tool's `environment` arg: explicit arg →
`default_environment` → sole available environment → error listing options.
The resolved set is the union of mode-A entries and mode-B discovered
environments (keyed by label and `environmentId`, cached in `ctx.state`).

## Work items

Every item lands with focused pytest coverage (httpx mocked via
`httpx.MockTransport`; no live server in unit tests) and keeps
`hermes plugins doctor . --ci` green. Tests live in `tests/` mirroring module
names.

### WI-1 — Repo scaffold and plugin skeleton *(foundation; inspect)*

Create: `plugin.yaml` (manifest v2, `name: t3code`, `kind: backend`,
`provides_tools` list, `config_schema` matching the Configuration section,
`python_dependencies: ["cryptography"]`), `pyproject.toml` (project
`hermes-plugin-talaria`, entry point group `hermes_agent.plugins` →
`t3code = "talaria"` package), package `talaria/__init__.py` with `register(ctx)`
that registers tools/CLI from stub modules, `schemas.py`, `tools.py`,
`config.py`, `errors.py`, `tests/conftest.py`, CI workflow running
`pytest` + `hermes plugins doctor . --ci` (doctor step marked
`continue-on-error` until hermes is installable in CI, then required).
AC: `register(ctx)` with a fake ctx registers every declared tool name; no
module opens a socket at import or registration (test asserts via a socket
guard fixture).

### WI-2 — Config + secret resolution (`talaria/config.py`)

`resolve_environments(ctx) -> dict[name, EnvironmentRef]` implementing the
resolution order above; `secret_name_for(env_name)`; `get_secret`/`set_secret`
wrappers using the scoped-read pattern; error types in `errors.py`
(`NotAuthenticated`, `EnvironmentNotFound`, `T3ApiError`) that serialize to
the JSON error contract with an actionable `hint` field (e.g. "run
`hermes t3code login <pairing-url>`").
AC: unit tests cover arg/default/sole/ambiguous resolution and secret-name
mangling (`my-laptop` → `T3CODE_TOKEN_MY_LAPTOP`).

### WI-3 — Environment HTTP client (`talaria/t3_env.py`) *(foundation; inspect)*

Mode-agnostic client over httpx (lazy singleton, one `httpx.Client` reused;
timeout default 30s): `descriptor()`, `shell()`, `thread(thread_id, turn_limit,
before_cursor)`, `dispatch(command: dict)`, `ws_ticket()`,
`exchange_pairing(base_url, subject_token)`, `exchange_dpop(base_url,
credential, dpop_signer)`. Auth is injected as a callable returning headers,
so mode A (static bearer) and mode B (DPoP token + per-request proof) plug in
without branching in call sites. Map 401 → `NotAuthenticated`, non-2xx →
`T3ApiError` with status + body excerpt. Pass through unknown response fields
untouched (NFR-5).
AC: mocked-transport tests for each method incl. 401 mapping and the pairing
exchange form encoding (exact `subject_token_type` string asserted).

### WI-4 — Mode A: pairing login CLI

`hermes t3code login <pairing-url> [--name <env-name>]`: parse the URL
(token is in the **hash**: `#token=…`; hosted variant carries the real origin
in a `?host=` query param — support both, mirroring t3code
`packages/shared/src/remote.ts` `resolveRemotePairingTarget`), fetch the
descriptor, run `exchange_pairing`, store the bearer secret and add/update the
environment entry (`ctx.set_config`), print scopes + expiry. `hermes t3code
logout <env>` deletes the secret.
AC: tests for both URL shapes, name defaulting (descriptor label slugified),
and that the token never appears in config — only in the secret store.

### WI-5 — Read tools: `t3_environments`, `t3_list`, `t3_thread`

Schemas in `schemas.py` (invest in `description` — the model routes on it),
handlers in `tools.py`. `t3_environments`: resolved environments with
descriptor label, mode, auth ok/expired, liveness (mode B adds relay status).
`t3_list`: shell snapshot condensed — per project: id, title, path; per
thread: id, title, `worktreePath`, `latestTurn` status, pending flags.
`t3_thread`: turn history with `turnLimit` (default 5), agent output text
extracted, `beforeCursor` passthrough for paging.
AC: golden-output tests from recorded shell/thread fixtures; oversized
responses truncated with an explicit `"truncated": true` marker.

### WI-6 — Write tools: `t3_new_thread`, `t3_prompt`, `t3_interrupt`, `t3_respond`

Build `ClientOrchestrationCommand` payloads exactly as transcribed above
(uuid4 `commandId`/`messageId`/`threadId`, ISO `createdAt`); `t3_prompt`
accepts optional `model_selection {instance_id, model}`, `runtime_mode`
(default `approval-required` — never default to `full-access`),
`interaction_mode`. `t3_respond` covers both `thread.approval.respond` and
`thread.user-input.respond` via a `kind` arg.
AC: dispatched payloads validated field-for-field against fixtures; defaults
asserted (`runtimeMode` default, empty attachments).

### WI-7 — `t3_wait` (poll-based)

Poll thread detail every `interval` (default 5s, floor 2s) until the latest
turn settles, an approval/user-input request appears, or `timeout` (default
300s) elapses; return the terminal state + last activity excerpt.
**Checkpoint CP-1:** verify against a live server that thread detail exposes
turn-settled status; fallback (decided): poll `GET /api/orchestration/shell`
and read that thread's `latestTurn`/pending flags, which are confirmed fields.
AC: tests with a scripted sequence of mocked responses covering settle,
approval-interrupt, and timeout paths (mock the clock; no real sleeps).

### WI-8 — Mode B step 1: Clerk headless OAuth (`talaria/auth_t3connect.py`, CLI `hermes t3code connect`)

Implement the 6-step flow from "Clerk headless OAuth" above, including
frontend-API-host derivation from the publishable key and the `"<code>.<state>"`
blob validation. Store Clerk tokens as secrets; implement refresh.
AC: tests for challenge/state generation, blob parse/reject (wrong state,
malformed), token exchange request shape, and pk→hostname derivation
(`pk_live_Y2xlcmsudDMuY29kZXMk` → `clerk.t3.codes`).

### WI-9 — Mode B steps 2-4: relay discovery + DPoP (`talaria/dpop.py` + auth_t3connect additions)

`dpop.py`: ES256 keypair (persist PEM as secret), JWK thumbprint, proof JWT
(`htm`, `htu`, `iat`, `jti`, optional `ath`) per RFC 9449. Relay client:
`list_environments()`, `dpop_token()`, `status(env)`, `connect(env)` →
credential; then `t3_env.exchange_dpop` at the tunnel base URL and a refresh
loop (re-exchange when <5 min remain; re-`connect` when the relay credential
expires). Discovered environments merge into `resolve_environments` with
cached copies in `ctx.state`.
AC: proof JWTs verified with the public key in tests (header `typ=dpop+jwt`,
alg ES256, claims); refresh state machine unit-tested with a mocked clock;
`t3_environments` shows discovered environments.

### WI-10 — M2: Effect RPC WebSocket client (`talaria/t3_ws.py`) *(foundation; inspect)*

Async client over `websockets`: connect via `ws_ticket()` →
`wss://…/ws?wsTicket=…`, JSON frames, request/response correlation by request
id, streaming-response support, reconnect with expo backoff (cap 30s), ping
interval 30s, auth-failure latching (stop reconnecting on ticket rejection —
mint a fresh ticket instead). Model on hermes' own
`gateway/relay/ws_transport.py`.
**Checkpoint CP-3 (blocking, first task of this item):** capture the literal
Effect RPC frame shape from a live t3code server (t3code's
`apps/server/integration/NetworkTransferMeasurement.integration.ts` drives a
raw client and can log frames; alternatively capture the official web client's
socket). Document the frame schema in `docs/design.md` before writing the
encoder. Fallback if frames prove unstable across t3 versions: pin a tested
t3code version range in README and gate on the descriptor's version field.
AC: round-trip against a recorded frame transcript (fixture-driven fake
server); reconnect/backoff tests with mocked clock (no real sleeps).

### WI-11 — M2: remote file tools

`t3_ls`, `t3_read_file`, `t3_write_file`, `t3_search` over
`projects.listEntries/readFile/writeFile/searchContents`. Byte-size caps on
reads (default 256 KiB, `"truncated"` marker) and writes require an explicit
`project_id` + path.
AC: fixture-driven tests per RPC incl. scope-error surfacing
(`EnvironmentAuthorizationError` → JSON error naming the missing scope).

### WI-12 — M2: live event push

Background reader (started lazily from an async tool `t3_watch <thread|shell>`,
via `ctx.spawn_task`) folding `subscribeThread`/`subscribeShell` items;
`afterSequence` cursor in `ctx.state`; salient events (turn settled, approval
requested, user-input requested) pushed via `ctx.inject_message`. Document and
fail-soft when `allow_gateway_injection` is not granted (return instructions
instead of silently dropping). `t3_unwatch` stops the reader (reverse state).
AC: reducer unit tests (sequence dedupe, snapshot replacement); injection
gated test (no grant → no inject, helpful JSON returned).

### WI-13 — Docs + distribution *(required docs item)*

Update in this repo: `README.md` (install, enable, both auth walkthroughs
incl. the pairing and connect ceremonies verbatim, config reference,
`allow_gateway_injection` note, tested t3code version), `docs/PRD.md` status
flips, `docs/design.md` gains the captured frame schema (WI-10). Add
`CONTRIBUTING.md` (dev setup, doctor, test commands). Prepare the
plugin-index submission (entry JSON with immutable ref) but do not submit
until M1+M2 are green — **Checkpoint CP-4:** the index repo URL
(`NousResearch/hermes-plugin-index`) 404'd on 2026-08-31; re-verify the
canonical index location at submission time (fallback: the in-hermes seed
`hermes_cli/data/plugin_index.json` documents the entry schema; promote via
Nous Discord `#plugins-skills-and-skins`).
AC: every config key and secret name that exists in code appears in README;
a fresh-machine walkthrough (install → enable → login → t3_prompt) has no
undocumented step.

### Deferred (explicitly out of scope now)

- Pairing-over-tunnel shortcut (**CP-2**): exchanging a `t3 pair` credential at the tunnel URL to skip Clerk/DPoP. Test once M1 lands; adopt only as an *additional* mode if it works.
- Terminals (`terminal:operate`), checkpoint revert, archive/snooze tools, attachments in `t3_prompt`.
- Any t3code-side changes: this plugin is a pure client.

## Acceptance criteria ↔ requirements map

| Requirement | Work items | Proven by |
|---|---|---|
| FR-1 | WI-5, WI-9 | `t3_environments` tests |
| FR-2, FR-3 | WI-5 | `t3_list`/`t3_thread` golden tests |
| FR-4, FR-5 | WI-6 | dispatch payload fixture tests |
| FR-6 | WI-7 | wait state-machine tests (CP-1 gate) |
| FR-7 | WI-4 | login CLI tests |
| FR-8 | WI-8 | OAuth flow tests |
| FR-9 | WI-9 | DPoP + refresh tests |
| FR-10 | WI-10, WI-11 | WS + file tool tests (CP-3 gate) |
| FR-11 | WI-12 | reducer + injection tests |
| FR-12 | WI-2, WI-9 | resolution tests |
| NFR-1..6 | all | socket-guard fixture, secret-location tests, doctor in CI |
