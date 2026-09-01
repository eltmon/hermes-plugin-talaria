# Talaria (hermes-plugin-talaria) — Design (rev 2)

Goal: a Hermes Agent plugin that lets a hermes-agent drive any T3 Code instance —
list projects/threads, start turns (send prompts), respond to approvals, read agent
output, read/write files in the environment's worktrees — distributable to other
Hermes users via the community plugin index.

**Topology (the actual deployment, rev 2 correction):** hermes runs on a remote
Fly.io machine; t3code instances run elsewhere — on the operator's laptop behind
NAT, possibly several instances linked via T3 Connect. Hermes and t3code are NOT
co-located. Consequences:

- The plugin only ever makes **outbound** HTTPS/WSS from wherever hermes runs. No
  inbound ports on the hermes host, no infra changes on the t3code host beyond
  what t3code itself provides (`t3 connect link` = cloudflared tunnel).
- Filesystem access to the t3code machine must go **through the t3code API**
  (`projects.readFile/writeFile/searchContents/listEntries`), which is
  WebSocket-RPC-only. So the WS milestone is core, not optional.
- T3 Connect is the primary connectivity + discovery mode, not a later add-on.

Source investigations: `/home/eltmon/Projects/hermes-agent` (main @ 2026-08-31) and
`/home/eltmon/Projects/t3code`.

## Auth & connectivity verdict

**T3 Connect is the right primary mode for this topology.** It solves both problems
a cloud-hosted hermes has: *reachability* (each linked environment gets a public
Cloudflare tunnel `httpBaseUrl`/`wsBaseUrl`) and *discovery* (the relay's
`GET /v1/environments` lists every environment linked to the account — exactly the
"multiple instances" case).

Key architectural fact that keeps the plugin simple: **the relay never proxies RPC
traffic.** It brokers a tunnel URL + a short-lived credential, then the client
talks directly to the environment using the same handshake every client uses
(`POST /oauth/token` → `POST /api/auth/websocket-ticket` → `wss://…/ws?wsTicket=…`).
So the plugin has ONE environment-API layer and two pluggable credential/discovery
front ends:

### Mode A — `direct` (simpler; LAN / Tailscale / any reachable URL)

For a t3code the hermes host can already reach (tailnet member, LAN, or a manually
known tunnel hostname). Bearer tokens, no Clerk, no DPoP:

1. User runs `t3 pair` on the t3code host, pastes the pairing URL into
   `hermes t3code login <pairing-url>` (token rides the URL **hash** —
   `buildPairingUrl`, `apps/server/src/startupAccess.ts:92`).
2. Plugin exchanges it (RFC 8693 at the environment's `/oauth/token`,
   `subject_token_type=urn:t3:params:oauth:token-type:environment-bootstrap`) →
   **30-day bearer** with `AuthStandardClientScopes` (`orchestration:read/operate`,
   `terminal:operate`, `review:write`, `relay:read`). Scope-subset rule means a
   paired client can never escalate to `access:*`.
3. All subsequent calls: `Authorization: Bearer`. Fully supported for direct
   connections per `docs/internals/environment-auth.md`.

Given the operator's infra, putting the Fly machine on the tailnet makes mode A
work against the laptop with zero relay involvement — good fallback and good for
self-hosters who don't use T3 Connect.

### Mode B — `t3connect` (primary; any linked instance, anywhere)

All feasible from Python (httpx + `cryptography` for ES256):

1. **Clerk sign-in, headless:** t3code's public PKCE OAuth client
   (`T3CODE_CLERK_CLI_OAUTH_CLIENT_ID`, scopes `openid profile email`). Same
   out-of-band flow `t3 connect login --headless` uses: print the hosted
   `/connect` authorize URL, user opens it in any browser, pastes the code back
   (`hermes t3code connect` CLI command). Store the Clerk refresh/access tokens.
2. **Discovery:** `GET <relay>/v1/environments` with the Clerk bearer →
   `{ environments: [{ environmentId, label, endpoint: { httpBaseUrl, wsBaseUrl }, linkedAt }] }`
   (`packages/contracts/src/relay.ts:621`). This is how the plugin enumerates all
   the operator's linked instances — no per-instance config needed.
3. **Relay DPoP token:** generate ES256 keypair, `POST /v1/client/dpop-token`
   (RFC 8693, `scope=environment:connect environment:status`).
4. **Connect grant:** `POST /v1/environments/:id/connect` with DPoP →
   short-lived environment **bootstrap credential bound to the JWK thumbprint**
   (`relay.ts:1022`) — so DPoP is mandatory on this path, not optional.
5. **Environment handshake (shared with mode A):** verify
   `/.well-known/t3/environment` `environmentId` matches, exchange the credential
   at the environment's `/oauth/token` with a DPoP proof → 1-hour DPoP-bound
   access token (`exchangeRemoteDpopAccessToken`,
   `packages/client-runtime/src/authorization/remote.ts:35`), refresh loop on
   expiry → `POST /api/auth/websocket-ticket` → connect.

Reference implementation to mirror: `packages/client-runtime/src/relay/managedRelay.ts`
(`listEnvironments` :696, `connectEnvironment` :795) and
`connection/resolver.ts:139` (`makeRelayBroker`).

**Checkpoint (pragmatic shortcut, verify on a live setup):** pairing *over the
tunnel* — exchange a `t3 pair` credential at the tunnel `httpBaseUrl` for a plain
30-day bearer and skip Clerk/DPoP entirely, treating the tunnel as transport only.
The environment likely can't distinguish tunnel from direct traffic, but this
depends on tunnel-hostname stability and on nothing (e.g. Cloudflare Access) being
in front. If it works it's a nice low-friction mode; if not, mode B is the answer.
Do not build the plugin's core around it either way.

## The T3 Code API surface the plugin uses

Protocol facts (verified against the repo, contracts v0.0.33; `@t3tools/contracts`
is `private: true`, NOT on npm — the Python plugin transcribes the JSON shapes):

- **HTTP API** (`packages/contracts/src/environmentHttp.ts`):
  - `GET /.well-known/t3/environment` — unauthenticated descriptor: `environmentId`, label, capabilities, sessionMethods.
  - `GET /api/orchestration/shell` — snapshot of all projects + threads (`OrchestrationShellSnapshot`). Per-thread: `id, projectId, title, branch, worktreePath, latestTurn, hasPendingApprovals, hasPendingUserInput, planProgress, …`
  - `GET /api/orchestration/threads/:threadId?turnLimit=N&beforeCursor=…` — thread detail + turn history.
  - `POST /api/orchestration/dispatch` — **all mutations**, same `ClientOrchestrationCommand` union as WS. Requires `orchestration:operate`.
  - `POST /api/auth/websocket-ticket` — bearer/DPoP → 5-min ticket for the WS.
- **WebSocket** `GET /ws?wsTicket=…` — Effect RPC, JSON-serialized
  (`RpcSerialization.layerJson`), ~95 methods in `WsRpcGroup`
  (`packages/contracts/src/rpc.ts:985`). Required for: **file ops**
  (`projects.listEntries/readFile/writeFile/searchContents/searchEntries` are
  WS-only), live streams (`orchestration.subscribeShell` / `subscribeThread` with
  `afterSequence` resume), terminals, preview automation.
- **Key commands** (`ClientOrchestrationCommand`, `packages/contracts/src/orchestration.ts:940`):
  `thread.create`, **`thread.turn.start`** (the "send prompt" — client-generated
  `commandId`/`messageId` UUIDs, ISO `createdAt`, `runtimeMode`,
  `interactionMode`, `modelSelection {instanceId, model}`),
  `thread.turn.interrupt`, `thread.approval.respond`, `thread.user-input.respond`,
  `thread.archive`, `thread.checkpoint.revert`, `thread.session.stop`, …

Traps found (do not re-derive):

- `WS_METHODS.projectsList/projectsAdd/projectsRemove` (`rpc.ts:198-200`) are
  **dead constants** — not in `WsRpcGroup`. Listing goes through the orchestration
  shell.
- No protocol-version header; compatibility is schema-side (`Schema.optional` +
  decode defaults). Tolerate unknown fields.
- The t3code MCP server (`POST /mcp`) is **preview automation only**, per-provider-
  session tokens — a dead end for driving the server.
- Best in-repo client templates: `apps/server/src/server.test.ts:990-1005,
  3388-3405` (minimal WS RPC client), `apps/server/src/cli/project.ts` (typed HTTP
  client), `apps/server/integration/NetworkTransferMeasurement.integration.ts`
  (raw WS frames — the empirical source for the Effect RPC frame shape).

## Hermes plugin shape

Per hermes' placement policy (CONTRIBUTING.md:88-101), an integration with someone
else's product ships as a **standalone repo**, installable with
`hermes plugins install eltmon/hermes-plugin-talaria` and listed on the community
index (`NousResearch/hermes-plugin-index`). Native plugin, not MCP: hermes' MCP
client speaks stdio/StreamableHTTP/SSE only, so bridging t3code's WS would add a
process for nothing.

```
hermes-plugin-talaria/
├── plugin.yaml        # manifest_version: 2, kind: backend, config_schema, provides_tools
├── __init__.py        # register(ctx): tools + CLI commands + on_unload. NO sockets here
│                      #   (hermes plugins doctor blocks outbound sockets during register())
├── schemas.py         # LLM-facing tool schemas
├── tools.py           # handlers: (args, **kwargs) -> JSON string, never raise
├── t3_env.py          # environment API: descriptor, shell, thread, dispatch, ws-ticket
├── t3_ws.py           # Effect-RPC-over-WS client (file ops, streams); reconnect/backoff
│                      #   modeled on hermes' gateway/relay/ws_transport.py
├── auth_direct.py     # mode A: pairing exchange, bearer storage/refresh
├── auth_t3connect.py  # mode B: Clerk PKCE headless OAuth, relay discovery, DPoP (ES256)
├── cli.py             # `hermes t3code login <pairing-url>`, `hermes t3code connect`,
│                      #   `hermes t3code environments`
├── pyproject.toml     # entry point: [project.entry-points."hermes_agent.plugins"]
│                      #   deps: httpx (core in hermes), websockets (core), cryptography
└── README.md          # incl. "run `hermes plugins enable t3code`" — plugins are opt-in
```

**Config — modes + multi-instance from day one:**

```yaml
plugins:
  entries:
    t3code:
      settings:
        t3connect:
          enabled: true            # mode B: relay discovery of all linked environments
          relay_url: https://relay.t3.codes   # default from t3code release config
        environments:              # mode A: explicitly configured direct instances
          laptop-tailnet: { base_url: "https://t3.tail1234.ts.net" }
        default_environment: null  # or a name / environmentId; else tools require the arg
```

Tools take an `environment` arg (name or `environmentId`); the resolved set is the
union of mode-A entries and mode-B discovered environments.

**Token/secret storage:** bearer tokens, Clerk tokens, and the DPoP private key are
secrets → NOT in `plugin-data/` or `config.yaml`. The CLI commands write them to
the hermes `.env` secret surface per environment/account; reads go through
`agent.secret_scope.get_secret` (with the `UnscopedSecretError` → `os.getenv`
fallback — canonical pattern, `plugins/platforms/homeassistant/adapter.py:43`). On
401, tools return a JSON error naming the re-auth command. Non-secret cursors
(last-seen `sequence` per thread, cached environment list) go in `ctx.state`.

### Milestone 1 — HTTP core + both auth modes

| Tool | Backing call |
|---|---|
| `t3_environments` | mode-B relay `GET /v1/environments` + mode-A config; per-env descriptor + `POST /v1/environments/:id/status` liveness |
| `t3_list` | `GET /api/orchestration/shell` → projects + threads incl. `worktreePath`, `hasPendingApprovals`, `latestTurn` |
| `t3_thread` | `GET /api/orchestration/threads/:id?turnLimit=N` — read history/output |
| `t3_new_thread` | dispatch `thread.create` |
| `t3_prompt` | dispatch `thread.turn.start` |
| `t3_interrupt` | dispatch `thread.turn.interrupt` |
| `t3_respond` | dispatch `thread.approval.respond` / `thread.user-input.respond` |
| `t3_wait` | poll thread detail until turn settles / approval pending, bounded timeout |

Shared lazy httpx client via `plugins/plugin_utils.py` `lazy_singleton` (hermes is
multi-threaded; hand-rolled `global` singletons are a documented footgun).

**Checkpoint (M1 gate):** verify against a live server that thread-detail HTTP
exposes enough turn status for `t3_wait` (shell's `latestTurn` +
`hasPendingApprovals` strongly suggest yes; fallback: poll the shell endpoint —
fields confirmed there).

### Milestone 2 — WebSocket: remote filesystem + live streams (core, not optional)

Hermes is remote, so this is the only route to the t3code machine's files:

- `t3_read_file`, `t3_write_file`, `t3_search`, `t3_ls` → `projects.readFile /
  writeFile / searchContents / searchEntries / listEntries` over WS RPC.
- `orchestration.subscribeThread` / `subscribeShell` with `afterSequence` resume
  (cursor in `ctx.state`) — push t3code events (turn finished, approval requested)
  into the hermes conversation via `ctx.inject_message`. Requires the user grant
  `plugins.entries.t3code.allow_gateway_injection: true`, and a running event
  loop — start the reader lazily from an async tool handler or as a platform
  adapter; **never** in `register()`.

Implementation: Python client for Effect RPC JSON framing over `websockets`
(already a core hermes pin, `pyproject.toml:125`). Empirical frame source: run
`NetworkTransferMeasurement.integration.ts` or capture a real client session.
Connection management modeled on hermes' own `gateway/relay/ws_transport.py`
(reconnect backoff, requestId correlation, ping_interval, revocation latching).

**Checkpoint (M2):** confirm `projects.*` file-op scope requirements
(`orchestration:read` for reads / `orchestration:operate` for writes per
`RpcAuthorization.ts`) hold through the DPoP-token path, and that WS tickets work
identically through the tunnel.

### Milestone 3 — polish for distribution

- Terminals (`terminal:operate`) if wanted; checkpoint revert; archive/snooze.
- Plugin-index submission with an immutable ref; README with both onboarding
  flows; `hermes plugins doctor . --ci` in CI.

## Checklist against the hermes plugin contract

- [ ] No socket during `register()` (doctor blocks it); `hermes plugins doctor . --ci` green
- [ ] Handlers `(args, **kwargs) -> str`, always JSON, never raise; hooks take `**kwargs`
- [ ] Secrets via `secret_scope`, settings via `ctx.get_config`, cursors via `ctx.state`,
      nothing written into the install dir
- [ ] `ctx.on_unload` closes clients; background readers via `ctx.spawn_task`
      (auto-cancelled on unload)
- [ ] `plugin.yaml` v2 with `config_schema`; `requires_env` only for genuinely
      env-provided values (tokens are written by the plugin's own CLI commands)
- [ ] README: `hermes plugins enable t3code` (opt-in), both auth walkthroughs, the
      `allow_gateway_injection` grant for event push
- [ ] Submit to `NousResearch/hermes-plugin-index` with an immutable ref once stable

## Implementation notes vs PRD

- **WI-1 directory loader:** PRD `files_scope` listed `talaria/__init__.py` as the register() home. Hermes' directory scanner / `hermes plugins doctor .` copies the plugin directory and loads *that* directory as a module (`hermes_cli/plugins.py` `_load_directory_module`). A repo-root `__init__.py` re-exports `talaria.register` so doctor and git-clone installs work; the pip entry point `t3code = "talaria"` still loads the package. Source wins.
- **WI-9 relay DPoP `client_id`:** PRD omitted it. t3code `RelayPublicClientId` is only `t3-web` | `t3-mobile`. The plugin sends `t3-web`.
- **WI-9 `POST /v1/client/dpop-token`:** PRD table said Clerk bearer + dpop proof. Source (`managedRelay.ts` `exchangeAccessToken`) sends the Clerk token as RFC 8693 `subject_token` and the proof in the `dpop` header, with no `Authorization` bearer. The plugin matches source.

## CP-1 — `t3_wait` poll endpoint (decided fallback)

`t3_wait` polls `GET /api/orchestration/shell` and reads that thread's `latestTurn` plus `hasPendingApprovals` / `hasPendingUserInput`. It does not poll thread detail.

Why: CP-1 asked to verify on a live server that `GET /api/orchestration/threads/:id` exposes settled status. No live server confirmation was available, so the PRD fallback is the live path. Those shell fields are confirmed on `OrchestrationThreadShell` / `OrchestrationLatestTurn` (`state`: `running` | `interrupted` | `completed` | `error`). A thread-detail variant stays `TODO(CP-1)` in `talaria/tools.py` until a live server shows settled status there.

## CP-3 — Effect RPC WebSocket frame schema (captured 2026-08-31)

Captured against a live T3 Code server (`serverVersion` **0.0.37**, loopback `http://127.0.0.1:3773`) after `POST /api/auth/websocket-ticket` and `ws://127.0.0.1:3773/ws?wsTicket=…`. Serialization is Effect `RpcSerialization.layerJson` (one JSON object per WebSocket text message; **not** JSON-RPC 2.0, **not** NDJSON). Tested t3code range for this encoder: **0.0.37**.

Handshake: HTTP `POST /api/auth/websocket-ticket` with the environment access token → `{ ticket, expiresAt }` → connect `ws(s)://{host}/ws?wsTicket={ticket}`.

Client → server envelopes (live):

```json
{"_tag":"Ping"}
{"_tag":"Request","id":"probe-1","tag":"server.probe","payload":{},"headers":[]}
{"_tag":"Request","id":"shell-1","tag":"orchestration.subscribeShell","payload":{"afterSequence":0,"requestCompletionMarker":true},"headers":[]}
{"_tag":"Ack","requestId":"shell-1"}
{"_tag":"Interrupt","requestId":"shell-1"}
{"_tag":"Request","id":"ls-2","tag":"projects.listEntries","payload":{"cwd":"<abs path>"},"headers":[]}
```

`Request.headers` is an array of `[name, value]` pairs (empty `[]` is valid). `Request.id` is a string. RPC method names are the `tag` strings from `WsRpcGroup` (`server.probe`, `projects.listEntries`, `orchestration.subscribeShell`, …).

Server → client envelopes (live):

```json
{"_tag":"Pong"}
{"_tag":"Exit","requestId":"probe-1","exit":{"_tag":"Success","value":{}}}
{"_tag":"Chunk","requestId":"shell-1","values":[{"kind":"snapshot","snapshot":{}}]}
{"_tag":"Exit","requestId":"shell-1","exit":{"_tag":"Failure","cause":[{"_tag":"Interrupt","fiberId":191893}]}}
{"_tag":"Exit","requestId":"ls-2","exit":{"_tag":"Success","value":{"entries":[{"path":"__init__.py","kind":"file"}],"truncated":false}}}
```

A missing required payload key (`projects.listEntries` without `cwd`) returned `Exit` / `Failure` / `cause: [{_tag:"Die","defect":"Missing key\\n  at [\"cwd\"]"}]`. Unary RPCs complete with a single `Exit`. Streaming RPCs emit one or more `Chunk` frames (`values` is a non-empty array of stream items) and finish with `Exit`. Interrupting a stream yields `Exit`/`Failure`/`Interrupt` with a numeric `fiberId`.

Keepalive: client `Ping` → server `Pong`. Also send WebSocket-level pings every 30s (hermes `ws_transport.py` pattern). After each incoming `Chunk`, the client sends `{"_tag":"Ack","requestId"}` before waiting for the next Chunk or Exit (Effect stream backpressure; the server latches until Ack). Encoded `Interrupt` is `{_tag, requestId}` only — no `interruptors` on the wire. Server `{"_tag":"Defect","defect":…}` and `{"_tag":"ClientProtocolError","error":…}` are terminal for in-flight waiters. On ticket rejection, stop reconnecting and mint a fresh ticket. Reconnect backoff exponential, cap 30s.

Sanitized transcripts (no live shell dump) live in `tests/fixtures/frames/`.
