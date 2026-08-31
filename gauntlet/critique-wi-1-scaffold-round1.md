# WI-1 scaffold — round 1
Verdict: PASS

## Criteria
- CORRECTNESS: Met. `plugin.yaml` is name `t3code`, kind `backend`, manifest v2, `python_dependencies: [cryptography]`, `provides_tools` is exactly the M1 set (`t3_environments`, `t3_list`, `t3_thread`, `t3_new_thread`, `t3_prompt`, `t3_interrupt`, `t3_respond`, `t3_wait`), and `config_schema` has `t3connect` / `environments` / `default_environment` with the PRD Configuration defaults (including public Clerk identifiers, empty environments map, null default environment). `pyproject.toml` is project `hermes-plugin-talaria` with entry point `hermes_agent.plugins` → `t3code = "talaria"`. `register(ctx)` registers those eight tools plus CLI `login|connect|environments|logout`. CI runs `pytest` and `hermes plugins doctor . --ci` with `continue-on-error`. Stubs in `config.py` / `errors.py` / tool bodies are in-scope for WI-1.
- CONTRACT: Met. Import and `register(ctx)` open no sockets (socket-guard test plus a live constructor-deny probe). Tool handlers are `(args, **kwargs) -> str`, always JSON `{"error", "hint"}`, and do not raise. No hooks are registered; `on_unload` matches Hermes' `Callable[[], None]`. `register(ctx)` registers tools and the `t3code` CLI and does no network I/O. Root `__init__.py` re-exports `register` so the directory loader/doctor can import the plugin dir; the pip entry point loads `talaria` instead. Real `doctor_plugin('.')` against the hermes-agent checkout registered 8 tools and returned ok.
- SECURITY: Met. No bearer/token/password in config, `ctx.state`, `plugin-data/`, logs, error strings, or tests. `plugin.yaml` only stores the PRD public Clerk identifiers and says they are not secrets; environment entries are `base_url` only. `runtime_mode` is documented as default `approval-required` and "do not use full-access unless asked" — it is not the default.
- TESTS: Met. `tests/test_scaffold.py` checks declared-vs-registered tool names, toolset `t3code`, CLI subcommands, JSON error contract with extra kwargs, and directory-loader re-export. `socket_guard` is a real deny (constructor + `connect`/`create_connection`); a dedicated test proves the deny fires, and import/register under it does not. No sleeps, no live HTTP, no live servers.
- SIMPLICITY: Met. Shape matches `plugins/spotify/` (manifest + `register()` loop + `(args, **kwargs)` JSON handlers) plus the google_meet CLI argparse tree the PRD asks for. Extra files (`cli.py`, `schemas.py`, `errors.py`, empty `config.py`, root `__init__.py`) are required by WI-1 or the directory loader, not speculative layers.

## Defects

## Probes run
- `python3 -m pytest tests/ -q --tb=short` → 6 passed in 0.01s (exit 0).
- `PYTHONPATH=/home/eltmon/Projects/hermes-agent python3 -c "from hermes_cli.plugin_dev import doctor_plugin; r=doctor_plugin('.'); print(r.format_text())"` → `manifest: t3code 0.1.0 (backend)`; `OK: runtime discovery, manifest parsing, import, and registration passed`; `registrations: 8 tool(s), 0 hook(s)`; `ok True`. One warning only: unpinned `cryptography`, which is the PRD-specified declaration (doctor `--ci` treats warnings as non-fatal).
- Installed `hermes` on PATH is v0.9.0 and has no `plugins doctor` subcommand (exit 2). That is why CI marks the doctor step `continue-on-error`.
- `yaml.safe_load(plugin.yaml)` → name/kind/manifest_version/tools/deps/config_schema keys and t3connect defaults match the PRD.
- Socket-deny probe: `import talaria` + `register(FakeCtx)` under patched `socket.socket` / `connect` / `create_connection` succeeds; subsequent `socket.socket()` raises the deny.
- All eight handlers: VAR_KEYWORD present; `json.loads(handler({...}, extra=1, ctx=None))` is `{"error": "not implemented", "hint": ...}`.
- `importlib.metadata` entry point `t3code -> talaria` loads `talaria.register`.
- `rg` over `talaria/` and `tests/`: no `sleep(`, `httpx`, `urllib`, `websocket`, tokens, or bearer strings.
