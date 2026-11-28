# Contributing to Talaria

Plugin id is `t3code`. Python 3.10+. Do not open sockets in `register()` —
`hermes plugins doctor` blocks that.

## Dev setup

```
git clone https://github.com/eltmon/hermes-plugin-talaria
cd hermes-plugin-talaria
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

That installs `httpx`, `cryptography`, `websockets`, and `pytest`. Hermes
Agent is not required for unit tests.

## Tests

From the repo root, with the venv active:

```
python -m pytest tests/ -q
```

Tests mock HTTP with `httpx.MockTransport` and fake WebSocket servers. They
do not talk to a live T3 Code or Clerk. Delay / retry tests use fake clocks
— do not add real `time.sleep` in tests.

## Plugin doctor

With [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed
and `hermes` on `PATH`, from this repo root:

```
hermes plugins doctor . --ci
```

Doctor copies the directory, loads it as a plugin, and runs `register(ctx)`
under a socket guard. `--ci` exits 1 on errors. Missing
`python_dependencies` (`cryptography`) are surfaced with a `pip install`
hint; Hermes never auto-installs them.

To exercise the plugin inside a real Hermes install without publishing:

```
hermes plugins install /absolute/path/to/hermes-plugin-talaria
hermes plugins enable t3code
```

Then `hermes t3code login <pairing-url>` or `hermes t3code connect` as in
the README.

## Disposable runtime acceptance (Windows)

The opt-in harness uses installed Hermes and stock T3 runtimes with fresh data
directories, a scrubbed child environment, and deterministic local model peers.
Run it with the project venv active (`httpx` is required). Supply the installed
Hermes dependency interpreter, its source checkout, the Electron executable,
and its server CLI entry. For an unpacked Node server, this harness needs an
adaptation; it currently targets the Windows Electron distribution.

```powershell
python scripts/e2e/run.py --root C:/talaria-qa-new `
  --hermes-source C:/Hermes/hermes-agent `
  --hermes-python C:/Hermes/installs/INSTALL/environments/ENV/venv/Scripts/python.exe `
  --t3-exe 'C:/path/to/T3 Code (Beta).exe' `
  --t3-entry C:/path/to/resources/server.asar/apps/server/dist/bin.mjs
```

The root must not exist. The harness stages the Git-tracked plugin payload into
that root's Hermes plugin directory, exercises the actual loader/doctor and
registered CLI/tool handlers, then runs a real Hermes agent conversation against
a loopback HTTP/SSE fixture. The real T3 server launches a local ACP fixture in
a disposable Git project. Neither fixture needs a model provider or account.
Hermes dependency provisioning is disabled; its existing interpreter is reused.
Hermes core files and installed dependencies are not modified.

Checks include all fourteen tools, pairing scopes, file list/read/write/search,
path traversal rejection, task results, repeated approvals, rejection, wait
timeout, interruption, conversation-scoped notification delivery, independent
unwatch, a real T3 restart while watching, bearer revocation and logout. The
notification receiver is instrumented through Hermes' real routing API; this
does not exercise Desktop rendering. RPC deadline behavior stays covered by the
fast synthetic network tests.

The `tool-discovery` stage uses Hermes' real Tool Search dispatcher to find the
plugin by its user-facing name, Talaria, and resolve a project/thread lookup to
`t3_list`. This catches installed tools that a chat cannot discover by the name
the user knows.

Results and payload hashes are written to `<root>/results/report.json`, with
per-stage JSON and logs beside it. The runner stops its verified owned server
tree and revokes its disposable bearer sessions, including on failure. Retain
the root privately: files named `*.private.*` and the profile may contain
disposable pairing credentials. An unsuccessful run is evidence of failure,
even if some stages completed. The runner refuses existing profiles and never
uses a signed-in Hermes or T3 profile.

This acceptance lane does not verify T3 Connect/Clerk/Cloudflare, live model
providers, Desktop UI, WSL, other operating systems, or independent T3-to-Hermes
delegation. Those need separate acceptance in their owning environments.

## Layout

| Path | Role |
|---|---|
| `plugin.yaml` | Manifest v2, `name: t3code`, `config_schema` |
| `__init__.py` | Re-exports `talaria.register` for directory / doctor loads |
| `talaria/` | Plugin package (tools, CLI, auth, WS client) |
| `tests/` | Pytest, one file per module |

Handlers return JSON strings and must not raise. Secrets go through
`talaria.config.set_secret`, never `config.yaml`.
