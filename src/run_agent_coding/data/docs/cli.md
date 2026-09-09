# Unified terminal command

Install the distribution and use `run`. The interactive frontend and print mode share `CodingApplication`, `CodingSession`, extensions and SQLite state.

```text
run
run "inspect this repository"
run --session <id>
run --print "explain the implementation"
run --print --mode json "explain the implementation"
run --state-dir <directory> --sessions
run --login <provider>
run gateway --help
run bench --help
```

`--provider`, `--model` and `--thinking` control model selection. Project `.env` values are loaded without overriding process environment variables. Use `--extension <path>` for explicit Session extensions, `--no-extensions` to disable automatic discovery, and `--project-extensions` to discover approved project extensions.

`run` defaults to a scrolling terminal with streaming replies, bounded tool previews, multiline input and history. Enter submits; Alt+Enter inserts a newline; Ctrl+C stops the current operation; Ctrl+D exits. `/expand <tool-call-id>` displays a full tool result. While a run is active, ordinary text becomes a correction at the next tool boundary; `/queue <text>` adds a follow-up.

Use `/help`, `/session`, `/new`, `/resume`, `/tree`, `/branch <entry-id>`, `/name`, `/model`, `/thinking`, `/compact`, `/reload` and `/export`. Session-changing commands require the current run to settle first. HTML reports are for reading; SQLite backups are for restoring state.

Without a TTY, pass `--print` explicitly. Pipe contents and an optional positional prompt form the input. Print JSON is one complete document, using snake_case fields; stdout has no per-event records or terminal controls. Diagnostics go to stderr. A model turn succeeds only after a durable completion receipt; cancellation and failed turns do not return success.

Sessions live in `~/.run/state.sqlite3` by default. `--state-dir` selects an isolated application state directory. New sessions, resumes and forks use the same database contract. No old session-file format, RPC mode, legacy command alias or Textual frontend is supported.
