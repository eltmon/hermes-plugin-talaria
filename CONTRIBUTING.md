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

## Layout

| Path | Role |
|---|---|
| `plugin.yaml` | Manifest v2, `name: t3code`, `config_schema` |
| `__init__.py` | Re-exports `talaria.register` for directory / doctor loads |
| `talaria/` | Plugin package (tools, CLI, auth, WS client) |
| `tests/` | Pytest, one file per module |

Handlers return JSON strings and must not raise. Secrets go through
`talaria.config.set_secret`, never `config.yaml`.
