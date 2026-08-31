# Talaria — drive T3 Code from Hermes Agent

**Talaria** (Hermes' winged sandals) is a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that lets a hermes-agent — running anywhere — drive one or more
[T3 Code](https://github.com/pingdotgg/t3code) instances: list projects and
threads, start turns (send prompts to coding agents), respond to approvals,
read agent output, and read/write files in the environment's worktrees.

The plugin id is `t3code`; the project name Talaria distinguishes this repo
from T3 Code itself.

```
hermes plugins install <owner>/hermes-plugin-talaria
hermes plugins enable t3code
```

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

## Status

Design/pre-implementation. See:

- [docs/PRD.md](docs/PRD.md) — product requirements + executable work items
- [docs/design.md](docs/design.md) — architecture investigation and decisions
- [spec.xbrief.json](spec.xbrief.json) — the machine-readable task plan (xBRIEF v0.8)

## License

[MIT](LICENSE). Not affiliated with Nous Research or T3 Tools Inc.
