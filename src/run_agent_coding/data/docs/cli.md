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
run gateway --help
run bench --help
```

`--provider`, `--model` and `--thinking` control model selection; the defaults come from `PROVIDER`, `MODEL` and `REASONING_EFFORT`, and endpoints and keys from `OPENAI_*` / `ANTHROPIC_*` (see docs/models.md). Project `.env` values are loaded without overriding process environment variables. Use `--extension <name-or-path>` for explicit Session extensions (built-in names: `experience`, `mcp`, `permission_policy`, `plan_mode`), `--trace` to record spans and report them with `/trace`, `--no-extensions` to disable automatic discovery, and `--project-extensions` to discover approved project extensions.

New CLI and Gateway sessions load all four built-ins by default: `experience`, `mcp`, `permission_policy` and `plan_mode`. Experience provides USER.md / MEMORY.md tools, managed Skills, automatic review and lifecycle maintenance. Plan mode starts off. MCP reads `<cwd>/.mcp.json` and registers stdio tools by their remote names. `--no-extensions` disables default and discovered extensions; explicit `--extension` names or paths still load, without duplicates. Project memory and Skills require the existing trust policy to approve the project (or an explicit `--trust-project`). Historical sessions keep their saved extension snapshot; use `run --session <id> --refresh-resources` or `run gateway --refresh-resources` to adopt current defaults while preserving history.

`run` opens a full-screen Textual chat with scrollable message history, streaming Markdown and expandable tool output. The separate `run_agent_coding.tui` module follows Tau's TUI structure while using the existing CodingApplication and JSONL session lifecycle. Use `run --no-tui` for the original terminal frontend. Print and JSON modes do not load the TUI.

Type `/` to browse commands and Tab to complete; Ctrl+P opens the command picker. `/model`, `/resume` and `/tree` open searchable selection dialogs. Enter submits (or accepts an active completion); Alt+Enter or Ctrl+J inserts a newline; Ctrl+C stops the current operation; Ctrl+D exits when the input is empty. Use `/theme` to change the theme and `/sidebar` or Ctrl+B to toggle the sidebar. Tool results can be expanded individually, or together with Ctrl+E; `/expand <tool-call-id>` opens a specific result.

While a run is active, ordinary text becomes a correction at the next tool boundary; `/queue <text>` adds a follow-up after the current task. Use Ctrl+C or `/stop` to stop the current operation.

Use `/help`, `/session`, `/new`, `/resume`, `/tree`, `/branch <entry-id>`, `/rewind <entry-id>`, `/fork <entry-id>`, `/name`, `/model`, `/thinking`, `/compact`, `/reload` and `/export`. Session-changing commands require the current run to settle first. HTML reports are for reading; copying workspace `.run/sessions/` and `gateway/*.jsonl` is for restoring state.

Without a TTY, pass `--print` explicitly. Pipe contents and an optional positional prompt form the input. Print JSON is one complete document, using snake_case fields; stdout has no per-event records or terminal controls. Diagnostics go to stderr. A model turn succeeds only after the transcript has been appended to the session JSONL; cancellation and failed turns do not return success.

Sessions live in `<cwd>/.run/sessions/<session-id>.jsonl` by default, with that directory's `index.jsonl` listing the workspace conversations. `--state-dir` selects a separate application state directory. Gateway routing and delivery replay `~/.run/gateway/sessions.jsonl` and `deliveries.jsonl`.

Persistent behavior settings live in `~/.run/settings.json`, with trusted `<cwd>/.run/settings.json` merged over them. Supported keys are `steeringMode`, `followUpMode` (`one-at-a-time` or `all`), `compaction.enabled`, `shellCommandPrefix`, and `defaultProjectTrust`. The last two are user-only. `--state-dir` relocates user settings and state, not the project's `.run` directory. Provider, model and thinking settings remain environment-based; see [Models](models.md) and [Security](security.md).

## Feishu gateway

`run gateway` serves the same coding agent over Feishu. It takes `--cwd`, `--state-dir`, `--provider`, `--model`, `--thinking`, `--extension`, `--no-extensions`, `--refresh-resources`, `--trust-project` and `--project-extensions` like `run`; everything else comes from the environment:

Only WebSocket long connections and text messages are supported. Keep `FEISHU_CONNECTION_MODE=websocket`, `FEISHU_MEDIA_ENABLED=false`, `FEISHU_STREAMING=false` and `FEISHU_TOOL_PROGRESS=off`; enabling unsupported modes is rejected at startup.

| Variable | Meaning |
| --- | --- |
| `FEISHU_APP_ID`, `FEISHU_APP_SECRET` | App credentials (required) |
| `FEISHU_DOMAIN` | `https://open.larksuite.com` for Lark; unset for Feishu |
| `FEISHU_ALLOWED_USERS` | Comma-separated open IDs allowed to talk to the bot |
| `FEISHU_ADMINS` | Open IDs exempt from the normal sender allow list |
| `FEISHU_ALLOW_ALL_USERS` | `true` to skip the allow list (development only) |
| `FEISHU_REQUIRE_MENTION` | Group messages must @-mention the bot (default `true`) |
| `FEISHU_GROUP_POLICY`, `FEISHU_GROUP_RULES` | Default group policy (`allowlist`) and optional per-chat JSON overrides |
| `FEISHU_REPLY_TO_MODE` | `first` (default), `all` or `off`: which chunks quote the user's message |
| `FEISHU_MAX_MESSAGE_LENGTH` | Chunk size for long replies (default 8000) |
| `GATEWAY_SESSION_RESET_MODE` | `none` (default), `idle`, `daily` or `both` |
| `GATEWAY_SESSION_RESET_IDLE_MINUTES`, `GATEWAY_SESSION_RESET_AT_HOUR` | Reset thresholds |
| `GATEWAY_GROUP_SESSIONS_PER_USER` | One session per participant in a group (default `true`) |
| `GATEWAY_THREAD_SESSIONS_PER_USER` | Per-user sessions inside threads (default `false`) |
| `GATEWAY_BUSY_INPUT_MODE` | `interrupt` (default), `queue` or `steer`; see below |
| `GATEWAY_BUSY_QUEUE_MAX_PENDING` | Maximum pending messages per chat (default 32); overflow receives a retry notice |
| `GATEWAY_UNAUTHORIZED_DM_BEHAVIOR` | `reply` (default, show open ID), `ignore` or `pair` |
| `GATEWAY_ADMIN_USERS`, `GATEWAY_USER_ALLOWED_COMMANDS` | Restrict slash commands when gateway admins are configured |
| `GATEWAY_AGENT_IDLE_SECONDS`, `GATEWAY_MAX_CACHED_AGENTS` | When and how many open agents to keep |
| `GATEWAY_TURN_LEASE_TIMEOUT_SECONDS` | How long a second chat waits for a session another chat is using (default 1800) |
| `GATEWAY_STALL_SECONDS` | Quiet time before a running turn is reported as stalled (default 300) |
| `GATEWAY_HEARTBEAT_POLL_SECONDS` | How often due heartbeats are checked (default 5) |
| `GATEWAY_HEARTBEAT_MIN_INTERVAL_SECONDS` | Minimum heartbeat interval (default 60) |
| `GATEWAY_DELIVERY_LEDGER` | Record replies before sending and resend unconfirmed ones after a restart (default `true`) |

Chat commands: `/new` (or `/reset`), `/stop`, `/status`, `/help`, `/heartbeat add|once <minutes> <prompt>`, `/heartbeat list`, `/heartbeat remove <id>`, plus the session commands `/model`, `/thinking` and `/compact`. By default, unauthorized senders get their open ID in a direct message and are ignored in groups.

While a chat is busy, `interrupt` cancels its current turn and starts the new message; `queue` processes pending messages individually in arrival order; `steer` tries to inject a correction into the current turn and queues the message if that is unavailable. Control commands such as `/stop` bypass this policy. Pending queues are bounded and kept in memory; they are not durable job queues.

Behind the commands: every turn takes a lease on its coding session, so two chats that resolve to one session take turns instead of interleaving writes; a reply is recorded in `~/.run/gateway/deliveries.jsonl` before it is sent and resent (with a visible recovered-reply marker when the first attempt may have landed) when the next process finds the previous one dead; heartbeats live in `~/.run/gateway/heartbeats.json` and wake the chat's session as internal messages. A turn with no agent event for `GATEWAY_STALL_SECONDS` gets one notice and is left to `/stop`; the notice itself does not cancel the turn. Gateway state paths also follow `--state-dir`.
