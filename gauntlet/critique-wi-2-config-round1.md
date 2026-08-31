# WI-2 config — round 1
Verdict: PASS

## Criteria
- CORRECTNESS: Met. `resolve_environment(ctx, name)` is explicit arg → `default_environment` → sole entry → `EnvironmentNotFound` listing options. Explicit wins over default; a set-but-unknown default does not fall through to sole; lookup accepts name or `environmentId`. `secret_name_for("my-laptop")` is `T3CODE_TOKEN_MY_LAPTOP` (uppercased, non-alnum → `_`). `NotAuthenticated`, `EnvironmentNotFound`, and `T3ApiError` inherit `TalariaError` and serialize via `to_json()` to `{"error", "hint"}` with an actionable hint (login command, available names/ids, HTTP status + body excerpt). `json_error(error, hint)` is unchanged; WI-1 stub tools still import and return it. Mode-B merge reads `ctx.state.get("discovered_environments")` (PluginState or dict); mode-A names win on collision.
- CONTRACT: Met. `talaria/config.py` and `talaria/errors.py` open no sockets; import and `register(ctx)` succeed under a constructor-deny socket patch. `get_secret` / `set_secret` lazy-import `agent.secret_scope` and `hermes_cli.config.save_env_value` inside the functions (ImportError → `os.getenv` / `os.environ`). No tool handlers were added in this item. Error types raise for callers to catch; they serialize, they are not returned from handlers here.
- SECURITY: Met. Secrets go through the wrappers only (`save_env_value` / scoped get / `os.getenv` fallback / in-memory `store=` test seam). `config.py` never calls `ctx.set_config`. Fixtures use `https://*.example.test` and the fake value `tok-test`. No bearer lives in config, `ctx.state`, or the repo.
- TESTS: Met. `tests/test_config.py` exercises explicit / default / sole / unknown / empty / ambiguous (hint lists `laptop`, `studio`, `env-studio`), hyphen mangling, discovered merge, mode-A override, and JSON error payloads. No sleeps, no live HTTP, no live servers. Two source-guard tests exist (no `set_config` substring; no top-level hermes import); the AC paths are behavioral, not copies of the implementation.
- SIMPLICITY: Met. One dataclass, `secret_name_for`, get/set wrappers matching the Home Assistant `UnscopedSecretError` → `os.getenv` pattern, and two resolve functions plus small parsers. Not a resolver framework. Extra surface is the WI-9 `ctx.state` hook, which the item explicitly allows. Diff is only the three owned files.

## Defects

## Probes run
- `python3 -m pytest tests/ -q --tb=short` → 26 passed in 0.02s (exit 0).
- `git status --short` / `git diff --name-only` → only `talaria/config.py`, `talaria/errors.py`, untracked `tests/test_config.py`.
- Socket-deny: delete `talaria*` from `sys.modules`, patch `socket.socket` / `create_connection` to raise, `import talaria.config` and `talaria.register(FakeCtx)` succeed (8 tools, CLI `t3code`).
- `json.loads(handle_t3_list({}, extra=1))` still `{"error": "not implemented", "hint": ...}`. All three error classes `json.loads(exc.to_json())` have both `error` and `hint`.
- Secret mangling probe: `my-laptop` → `T3CODE_TOKEN_MY_LAPTOP`.
- `get_secret` without hermes: `os.getenv` fallback and default. Injected fake `agent.secret_scope.get_secret` that raises `UnscopedSecretError` falls back to `os.getenv` (one call). Injected success returns the scoped value. `store=` bypasses the scoped path.
- `PYTHONPATH=/home/eltmon/Projects/hermes-agent` read-only: `get_secret("T3CODE_TOKEN_PROBE_RO")` returns the env value (same as `agent.secret_scope.get_secret`). Did not call `set_secret` on this path.
- PluginState-shaped `ctx.state.get`: discovered row with camelCase `environmentId` merges as mode `t3connect` and resolves by id.
- Invalid specs (`base_url` missing / blank / non-mapping) are dropped; a blank `default_environment` is treated as unset (ambiguous when two remain).
