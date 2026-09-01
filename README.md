# Talaria — drive T3 Code from Hermes Agent

**Talaria** (Hermes' winged sandals) is a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that lets a hermes-agent — running anywhere — drive one or more
[T3 Code](https://github.com/pingdotgg/t3code) instances: list projects and
threads, start turns (send prompts to coding agents), respond to approvals,
read agent output, and read/write files in the environment's worktrees.

The plugin id is `t3code`; the project name Talaria distinguishes this repo
from T3 Code itself.

**Status:** Milestone 1 (HTTP + both auth modes) and Milestone 2 (WebSocket
file tools + live watch) are implemented. Tested against **t3code 0.0.37**.

```
hermes plugins install eltmon/hermes-plugin-talaria
hermes plugins enable t3code
```

Plugins are opt-in. Install leaves the plugin disabled until you run
`hermes plugins enable t3code` (or answer yes if install prompts
`Enable now?`). Restart Hermes after enabling so the `t3_*` tools and
`hermes t3code` CLI load.

Hermes never auto-installs `python_dependencies`. This plugin declares
`cryptography` (ES256 DPoP for T3 Connect). If `hermes plugins doctor` or
install prints that it is missing, install it into the same environment that
runs `hermes`:

```
pip install cryptography
```

Direct pairing (`hermes t3code login`) does not need `cryptography`. T3
Connect (`hermes t3code connect`) does.

## Why

A hermes-agent in the cloud (say, a Fly.io machine) and your T3 Code on a
laptop behind NAT can't see each other. Talaria bridges them with
outbound-only connections from wherever hermes runs:

- **T3 Connect mode (primary):** discovers every environment linked to your
  T3 Connect account via the relay, and reaches each one through its
  Cloudflare tunnel. Auth is Clerk OAuth (headless PKCE) + DPoP.
- **Direct mode:** any t3code URL the hermes host can already reach (LAN,
  Tailscale). Auth is a `t3 pair` pairing URL exchanged for a 30-day bearer.

Either way, talaria ends up speaking the same T3 Code environment API:
HTTP for orchestration (list/prompt/approve), WebSocket (Effect RPC) for
remote file access and live event streams.

## Fresh machine: install → enable → login → t3_prompt

This is the shortest path to sending a prompt. The hermes host must be able
to reach the T3 Code URL (same LAN, Tailscale, or a tunnel you already
have). No `config.yaml` edits are required for this path — `login` writes
the environment entry and stores the bearer as a hermes secret.

1. Install Hermes Agent if you do not already have it, then:

   ```
   hermes plugins install eltmon/hermes-plugin-talaria
   hermes plugins enable t3code
   ```

2. Restart Hermes if it is already running.

3. On the machine that runs T3 Code (tested **t3code 0.0.37**), start the
   server and print a pairing URL:

   ```
   t3 pair
   ```

4. On the hermes host, paste that URL into:

   ```
   hermes t3code login <pairing-url>
   ```

   Optional: `hermes t3code login <pairing-url> --name laptop` to pick the
   stored name. Otherwise the name is the descriptor label slugified
   (`My Laptop` → `my-laptop`). The pairing token lives in the URL **hash**
   (`#token=…`); a hosted pairing URL may also carry `?host=` for the real
   origin. The bearer is stored as `T3CODE_TOKEN_<NAME>` (see [Secrets](#secrets)),
   never in `config.yaml`.

5. Start a Hermes session and ask it to send a prompt. Typical tool
   sequence with one paired environment (no `environment` argument needed):

   - `t3_environments` — confirm the pair is `auth: ok` and `live: true`
   - `t3_list` — pick a `thread_id` (or `t3_new_thread` in a project)
   - `t3_prompt` with that `thread_id` and your text
   - `t3_wait` if you want the turn to settle before reading output

There is no extra config, env-var, or secret-export step. If a tool returns
`not authenticated`, re-run `hermes t3code login <pairing-url>`.

## Auth walkthroughs

### Direct pairing — `hermes t3code login <pairing-url>`

Use this when the hermes host can already open the T3 Code HTTP URL.

On the T3 Code host:

```
t3 pair
```

On the hermes host (plugin enabled):

```
hermes t3code login <pairing-url>
```

That command is verbatim: the subcommand is `login`, and the argument is
the pairing URL `t3 pair` printed. `--name <env-name>` is optional.

To drop a pairing:

```
hermes t3code logout <env>
```

### T3 Connect — `hermes t3code connect` / `--code`

Use this when T3 Code instances are linked through T3 Connect (tunnels +
relay discovery). Link the environment on the T3 Code side first
(`t3 connect link` / the T3 Connect app), then sign Hermes in.

On the hermes host:

```
hermes t3code connect
```

It prints an authorize URL (hash params, no local callback port). Open that
URL in any browser, sign in, and copy the `"<code>.<state>"` blob from the
hosted callback page. Then:

```
hermes t3code connect --code '<paste the code here>'
```

Those two commands are verbatim: first `hermes t3code connect` with no
flags (prints the URL), then `hermes t3code connect --code` with the pasted
blob. A successful exchange prints `Connected to T3 Connect` and stores
Clerk tokens plus a DPoP key as hermes secrets (see [Secrets](#secrets)).

After connect, `t3_environments` lists every linked environment. Tools take
an optional `environment` argument (name or `environmentId`).

## Tools

| Tool | What it does |
|---|---|
| `t3_environments` | Configured + discovered environments, auth and liveness |
| `t3_list` | Projects and threads (`worktreePath`, `latestTurn`, pending flags) |
| `t3_thread` | Turn history and extracted agent output |
| `t3_new_thread` | Create a thread in a project |
| `t3_prompt` | Start a turn (`thread.turn.start`) |
| `t3_interrupt` | Interrupt the in-progress turn |
| `t3_respond` | Answer approval or user-input requests |
| `t3_wait` | Poll until the turn settles or a pending request appears |
| `t3_ls` / `t3_read_file` / `t3_write_file` / `t3_search` | Remote files over Effect RPC WebSocket |
| `t3_watch` / `t3_unwatch` | Live shell/thread events; see [Event injection](#event-injection-allow_gateway_injection) |

`runtime_mode` on `t3_prompt` / `t3_new_thread` defaults to
`approval-required`. Do not use `full-access` unless asked.

CLI: `hermes t3code login`, `hermes t3code connect`,
`hermes t3code logout <env>`. `hermes t3code environments` is a stub; use
the `t3_environments` tool.

## Configuration

Settings live under `plugins.entries.t3code.settings` in Hermes
`config.yaml`. Tokens never go here.

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
        environments:
          laptop: { base_url: "https://t3.tail1234.ts.net" }
        default_environment: null
        allow_gateway_injection: false
```

| Key | Default | Meaning |
|---|---|---|
| `t3connect.enabled` | `true` | When true, T3 Connect discovery and DPoP connect are on. Set `false` to use only direct `environments`. |
| `relay_url` | `https://relay.t3.codes` | T3 Connect relay origin (`t3connect.relay_url`). |
| `hosted_app_url` | `https://app.t3.codes` | Hosted app used to build the connect authorize URL (`t3connect.hosted_app_url`). |
| `clerk_publishable_key` | `pk_live_Y2xlcmsudDMuY29kZXMk` | Public Clerk identifier (`t3connect.clerk_publishable_key`). Not a secret. |
| `clerk_oauth_client_id` | `hzxSgY2cH10sDU2r` | Public CLI OAuth client id (`t3connect.clerk_oauth_client_id`). Not a secret. |
| `environments` | `{}` | Direct (mode A) map of name → `{ base_url }`. `login` upserts this; you can also set it by hand. Tokens are **not** stored here. |
| `default_environment` | `null` | Name or `environmentId` used when a tool omits `environment`. If null and more than one environment exists, tools error and list options. |
| `allow_gateway_injection` | `false` | Settings key `plugins.entries.t3code.settings.allow_gateway_injection`. Must be boolean `true` (not the string `"true"`) for `t3_watch` to inject events. |

Resolution for a tool's `environment` arg: explicit argument →
`default_environment` → the sole available environment → error listing
options. The resolved set is the union of `environments` and T3 Connect
discoveries.

## Event injection (`allow_gateway_injection`)

`t3_watch` pushes salient T3 Code events (turn settled, approval requested,
user-input requested) into the Hermes conversation via `ctx.inject_message`.

That requires **both**:

1. The **settings** key (what Talaria actually reads):

   ```yaml
   plugins:
     entries:
       t3code:
         settings:
           allow_gateway_injection: true
   ```

   Path: `plugins.entries.t3code.settings.allow_gateway_injection`. Boolean
   `true` only — `"true"` / `1` is treated as denied.

2. The Hermes **host grant** (sibling of `settings`, required for gateway
   `inject_message`):

   ```yaml
   plugins:
     entries:
       t3code:
         allow_gateway_injection: true
   ```

Without the settings grant, `t3_watch` does not inject anything. It returns
JSON with `watched: false` and instructions naming the settings key. Default
is off.

## Secrets

Written by the CLI into the hermes secret surface (`~/.hermes/.env` via
`save_env_value`). Never put these in `config.yaml`, `plugin-data/`, or the
install dir.

| Secret | Written by | Purpose |
|---|---|---|
| `T3CODE_TOKEN_<NAME>` | `hermes t3code login` | Per-environment 30-day bearer. `<NAME>` is the environment name uppercased with non-alphanumerics turned into `_` (`my-laptop` → `T3CODE_TOKEN_MY_LAPTOP`). |
| `T3CODE_CLERK_ACCESS_TOKEN` | `hermes t3code connect --code` | Clerk access token (T3 Connect). |
| `T3CODE_CLERK_REFRESH_TOKEN` | `hermes t3code connect --code` | Clerk refresh token (T3 Connect). |
| `T3CODE_CLERK_PKCE_PENDING` | `hermes t3code connect` (start) | Short-lived PKCE verifier/state while you paste the code. Removed after a successful `--code` exchange. |
| `T3CODE_DPOP_KEY` | first T3 Connect DPoP use | ES256 private key, stored as base64(PKCS8 PEM). |

Reads go through `agent.secret_scope.get_secret` with an `os.getenv`
fallback. On 401, tools return JSON naming the re-auth command
(`hermes t3code login <pairing-url>` or `hermes t3code connect`).

## Compatibility

WebSocket file tools and `t3_watch` speak Effect RPC JSON frames captured
against **t3code 0.0.37**. Newer T3 Code releases that keep that frame shape
should work; if frames change, pin 0.0.37 until Talaria is updated.

## Community index (not submitted)

Draft index entry: [docs/plugin-index-entry.json](docs/plugin-index-entry.json).
`ref` is the placeholder `REPLACE_WITH_HEAD_SHA`. Do not submit until the
operator chooses the live target. The working index as of 2026-09-01 is
[Revell-ai/hermes-plugin-index](https://github.com/Revell-ai/hermes-plugin-index);
Hermes' default URL
(`https://raw.githubusercontent.com/NousResearch/hermes-plugin-index/main/index.json`)
still 404s. Details: [docs/plugin-index-entry.md](docs/plugin-index-entry.md).

## License

[MIT](LICENSE). Not affiliated with Nous Research or T3 Tools Inc.

Design notes: [docs/PRD.md](docs/PRD.md), [docs/design.md](docs/design.md).
Dev setup: [CONTRIBUTING.md](CONTRIBUTING.md).
