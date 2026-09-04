# Run Agent CLI and commands

Run Agent supports print mode, Pi-compatible JSONL RPC mode, and a Textual interactive
TUI. `run-agent` opens the TUI by default. Print mode is selected with `-p/--print` or
`--mode` and uses the same staged session/provider preparation as the TUI. The
CLI entry point is `run_agent_coding.cli:app`.

## Common flags

```text
run-agent [OPTIONS] [PROMPT]
```

- `-p, --print`: run one prompt without the TUI.
- `--mode text|json|transcript`: choose print output and imply print mode.
- `--provider NAME`: select an explicit provider.
- `-m, --model ID`: select an explicit model.
- `-t, --thinking LEVEL`: set the initial thinking level for this run
  (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`). Overrides
  remembered and catalog defaults without persisting them; an unsupported level
  for the selected model is an error listing the available modes.
- `--session ID`: resume a session in the TUI or print mode.
- `--cwd PATH`: set the coding-session working directory.
- `-e, --extension PATH`: load an explicit extension.
- `--no-extensions`: disable discovered extension directories.
- `--project-extensions`: opt in to trusted project extensions after approval.
- `-a, --approve` / `-na, --no-approve`: run-only project-trust decisions.

Explicit `--provider` and `--model` overrides take precedence over a resumed
provider-aware transcript entry. Print mode reports actionable errors instead of
opening an interactive login flow.

## Model catalog refresh

```bash
run-agent update --models
```

This forces ETag revalidation of models.dev and the live NVIDIA model filter,
then atomically caches the transformed catalog at `~/.run/models-store.json`.
Opening `/model` performs the same refresh in the background, subject to a
four-hour freshness window. Cached/bundled models remain available on failure;
set `RUN_AGENT_OFFLINE=1` to disable catalog network access.

## Safety boundary

Project trust controls ambient project-resource loading; it is not a sandbox.
Explicit and discovered extensions execute as trusted package code. See
`security.md`.
