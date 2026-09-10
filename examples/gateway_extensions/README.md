# Gateway channel extensions

`setup_gateway(api)` registers trusted channel adapters using API version 2. The
Gateway owns their input streams and shutdown. Session extensions use the separate
`setup(api)` contract and never start a channel listener.

## Feishu setup

Create a Feishu enterprise app with a bot. Grant `im:message.p2p_msg:readonly`,
`im:message.group_at_msg:readonly` and `im:message:send_as_bot`; enable long
connection and subscribe to `im.message.receive_v1`. Publish the app and add its
bot to the target group. Group messages must mention the bot.

Set `FEISHU_APP_ID` and `FEISHU_APP_SECRET`, and configure the model provider.
`FEISHU_DOMAIN=https://open.larksuite.com` selects Lark. An optional
`FEISHU_INSTANCE_ID` distinguishes multiple adapter instances (default: `feishu`).

Create `gateway-identities.json` with explicit mappings. `account_id` is the app
ID, `sender_id` is the verified Feishu open ID, and `workspace` is relative to the
mapping file or an absolute path:

```json
[
  {
    "adapter_instance_id": "feishu",
    "account_id": "cli_replace_with_app_id",
    "sender_id": "ou_replace_with_open_id",
    "principal_id": "local-user",
    "workspace": ".",
    "conversation_scope": "sender"
  }
]
```

`sender` isolates each sender within a chat/thread. Explicit `shared` mappings
can share the route; all participants sharing it must map to the same principal.
Unmapped senders are rejected. Channel messages cannot supply internal session
IDs, principals or workspace paths. Changing an established route's workspace
requires an explicit new binding; it cannot silently run against a different
directory from the one protected by the task's lease.

```powershell
uv pip install -e ".[feishu]"
run gateway `
  --extension examples/gateway_extensions/feishu.py `
  --identity-map gateway-identities.json `
  --state-dir .run/gateway `
  --cwd .
```

## Current commands

| Input | Behavior |
| --- | --- |
| text / `/queue text` | Persist a foreground task and send its accepted receipt |
| `/steer text` | Bind to the active foreground run; idle input becomes a normal queued task |
| `/status [task_id]` / `/tasks` | Query principal-visible tasks, execution and workspace state without a model call |
| `/stop` | Cancel queued and running foreground tasks in the current session |
| `/cancel task_id` | Cancel one principal-visible task |
| `/new` | Stop the old foreground work; switch to a new session after actual cleanup |

Stopping controls first receive a `stopping`/`cancelling` receipt. Their final
`stopped`/`cancelled` receipt follows confirmed reservation release. `/new` returns
the new session ID and conversation epoch after that same boundary. Commands and
tasks have persistent source-message deduplication. Normal input is rejected while
a route replacement is pending.

Steering first sends `accepted` with a stable task ID and target run. At the next
consumption boundary, the input message, any preceding intermediate reply and a
`consumed` receipt commit together. Consumed means recorded in the run's durable
history, not that a model request or the requested work succeeded. `/status`
exposes the target run and consumed entry ID; the target run reports its own result.

If the target finishes before consuming the input, the same task becomes queued
at its original arrival position and sends a queued notification followed by its
eventual result. Waiting and delivery capacity are reserved at acceptance. Stop
and new-session controls cancel pending steering in the old session; cancellation
of one steering task leaves its target run and other inputs running.

The adapter has separate bounded ordinary/control ingress and callback capacity.
SDK transport acknowledgement is not business acceptance; only a persisted
accepted receipt means the Gateway owns the task. SDK input overload is recorded
in logs and does not produce a false accepted receipt.

Results are persisted with their original destination and delivered through the
Outbox. Each Feishu text chunk uses a stable UUID derived from the delivery ID.
Retries retain those UUIDs and original reply/thread targets. This uses the
channel's idempotency window; it does not promise end-to-end exactly-once delivery.

`/background <content>` captures a clean Git commit and the source session's active
history, then runs in a separate detached worktree and session. First use initializes
source resources without making a model request. Skills remain pinned; changed prompt
contributions or trust policy are rejected. Background results retain the original
session and channel after `/new`, with a binary patch and untracked-file manifest.
Outputs are not merged automatically. Dirty/non-Git sources, submodules and symlinks
are not supported. Result capture accepts at most 1024 untracked files and 32 MiB total.
This is workspace separation, not a sandbox for arbitrary shell commands.

Preparation is bounded to two requests and control commands continue while it runs.
Stop/new invalidate unfinished preparation; shutdown drains preparation reservations.
Completed message deduplication does not consume preparation capacity.

Crash recovery currently retains unknown
executions and quarantines their workspaces; automatic process reconciliation and
the operator recovery command are still under implementation.

## Adapter contract

An adapter provides a unique `name`, `messages()` yielding `InboundMessage`,
`send(Delivery)` returning a JSON receipt, and `close()`. It validates the transport
identity and passes account, sender, chat, thread and source-message IDs. The host
maps these to an internal principal and route. `BoundedIngress` and
`QueueGatewayAdapter` provide the same contract for local integration tests.

The Gateway uses schema version 4 initialization only. Old development schemas and
API version 1 are not migrated or adapted.
