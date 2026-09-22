# Unified terminal command

Install the distribution and use `run`. The interactive frontend and print mode share `CodingApplication`, `CodingSession`, extensions and JSONL session files.

```text
run
run --no-tui
run "inspect this repository"
run --session <id>
run --print "explain the implementation"
run --print --mode json "explain the implementation"
run --state-dir <directory> --sessions
run --providers
run bench --help
```

`--provider`, `--model` and `--thinking` control model selection; the defaults come from `PROVIDER`, `MODEL` and `REASONING_EFFORT`, and endpoints and keys from `OPENAI_*` / `ANTHROPIC_*` (see docs/models.md). Project `.env` values are loaded without overriding process environment variables. Use `--extension <name-or-path>` for explicit Session extensions (built-in names: `compaction`, `experience`, `mcp`, `memory`, `permission_policy`, `plan_mode`; a name wins only when no such relative path exists), `--trace` to record spans and report them with `/trace`, `--no-extensions` to disable automatic discovery, and `--project-extensions` to discover approved project extensions.

New CLI sessions load all six built-ins by default: `experience`, `memory`, `compaction`, `mcp`, `permission_policy` and `plan_mode`. Experience owns verifier-gated Skill candidates and never writes a formal Skill without a host-owned passing report. Memory owns `USER.md` / `MEMORY.md`, the `memory` tool and `/memory`. Compaction owns the four-layer strategy (`/force-snip`, `/four-layer-compact`) and only rewrites requests while `compaction.strategy` is `four-layer`. Plan mode starts off. MCP reads `<cwd>/.mcp.json` and registers stdio tools by their remote names. `--no-extensions` disables default and discovered extensions; explicit `--extension` names or paths still load, without duplicates. Project memory, probes and Skills require the trust policy to approve the project (or an explicit `--trust-project`). Historical sessions keep their saved extension snapshot; use `run --session <id> --refresh-resources` to adopt current defaults while preserving history.

`run` opens a full-screen Textual chat with scrollable message history, streaming Markdown and expandable tool output. The separate `run_agent_coding.tui` module follows Tau's TUI structure while using the existing CodingApplication and JSONL session lifecycle. Use `run --no-tui` for the original terminal frontend. Print and JSON modes do not load the TUI.

Type `/` to browse commands and Tab to complete; Ctrl+P opens the command picker. `/model`, `/resume` and `/tree` open searchable selection dialogs. Enter submits (or accepts an active completion); Alt+Enter or Ctrl+J inserts a newline; Ctrl+C stops the current operation; Ctrl+D exits when the input is empty. Use `/theme` to change the theme and `/sidebar` or Ctrl+B to toggle the sidebar. Tool results can be expanded individually, or together with Ctrl+E; `/expand <tool-call-id>` opens a specific result.

While a run is active, ordinary text becomes a correction at the next tool boundary; `/queue <text>` adds a follow-up after the current task. Use Ctrl+C or `/stop` to stop the current operation.

Use `/help`, `/session`, `/new`, `/resume`, `/tree`, `/branch <entry-id>`, `/rewind <entry-id>`, `/fork <entry-id>`, `/name`, `/model`, `/thinking`, `/compact`, `/memory show|add|replace|remove`, `/force-snip`, `/four-layer-compact [instructions]`, `/reload`, `/evolve ...` and `/export`. Session-changing commands require the current run to settle first. HTML reports are for reading; copying workspace `.run/sessions/` is for restoring state.

Without a TTY, pass `--print` explicitly. Pipe contents and an optional positional prompt form the input. Print JSON is one complete document, using snake_case fields; stdout has no per-event records or terminal controls. Diagnostics go to stderr. A model turn succeeds only after the transcript has been appended to the session JSONL; cancellation and failed turns do not return success.

Sessions live in `<cwd>/.run/sessions/<session-id>.jsonl` by default, with that directory's `index.jsonl` listing the workspace conversations. `--state-dir` selects a separate application state directory.

Persistent behavior settings live in `~/.run/settings.json`, with trusted `<cwd>/.run/settings.json` merged over them. Supported keys are `steeringMode`, `followUpMode`, `compaction.enabled`, `compaction.strategy` (`cheap-first` default, `summary-only` for comparison, or `four-layer` to let the `compaction` extension own L1-L4 while the core keeps only the hard window guard and the durable commit; with the extension absent the request is left alone), `shellCommandPrefix`, and `defaultProjectTrust`. The last two are user-only. `--state-dir` relocates user settings and state, not the project's `.run` directory. Provider, model and thinking settings remain environment-based; see [Models](models.md) and [Security](security.md).
