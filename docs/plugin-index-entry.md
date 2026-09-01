# Plugin index entry (draft — do not submit)

Sibling of [`plugin-index-entry.json`](plugin-index-entry.json).

**Current PR target (HTTP 200 as of 2026-09-01):**
[Revell-ai/hermes-plugin-index](https://github.com/Revell-ai/hermes-plugin-index)
at `https://raw.githubusercontent.com/Revell-ai/hermes-plugin-index/main/index.json`.
Open a PR that adds this entry to that repo's `index.json`.

Hermes' code default is still
`https://raw.githubusercontent.com/NousResearch/hermes-plugin-index/main/index.json`
([NousResearch/hermes-plugin-index](https://github.com/NousResearch/hermes-plugin-index)).
That URL still 404s. Bare-name install/search only hit Revell-ai after
`plugins.index_url` is set to the Revell-ai raw URL. Until then the fallback is
the bundled seed `hermes_cli/data/plugin_index.json` and Nous Discord
`#plugins-skills-and-skins`.

`ref` is the placeholder `REPLACE_WITH_HEAD_SHA`. The index requires a
40-character commit SHA. Before opening a PR, replace that string with
`git rev-parse HEAD` from the commit you want pinned. Do not submit until
the operator chooses the live target, and do not submit the placeholder.
