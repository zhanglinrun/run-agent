# `claude_compaction` — four-layer compaction extension

A port of Claude Code's four-layer context pipeline (`src/services/compact/**`
in the reference TypeScript tree) onto Run Agent's extension seam. The package is
self-contained and is the built-in `compaction` extension
(`run_agent_extensions.BUILTIN_EXTENSIONS`): new sessions load it by default, and it
can also be loaded explicitly with `--extension compaction` or
`--extension src/run_agent_extensions/claude_compaction`. It rewrites requests only
while `compaction.strategy = "four-layer"` is configured.

Write side effects are limited to three things: this extension's own
`CustomEntry` records, notifications, and the request view it returns from
`before_provider_request`. Session history, the session JSONL and the durable
transcript are never rewritten by this package.

## The four layers

`before_provider_request` runs the pipeline once per request, in order
`L1 → L2 → L3/L4`, and requests **at most one** compaction commit per request.

| Layer | Scope | Model call | What it does |
| --- | --- | --- | --- |
| L1 microcompact | tool result | no | Replaces the content of old compactable tool results with `[Old tool result content cleared]`. |
| L2 snip | message | no | Drops every message named by a recorded snip boundary, then nudges the model when the view is long. |
| L3 session memory | session | no | Reuses `MEMORY.md` / `USER.md` content as the summary. |
| L4 auto/compact | session | yes | Summarizes the prefix through `services.inference.complete` and keeps a round-aligned recent tail. |
| reactive | session | yes | One emergency L4 attempt after the provider reported a context-overflow / media-too-large error. |

### L1 — microcompact (`micro.py`)

Final constants (all overridable, see *Configuration*):

* compactable tool set: `Read`, `Bash`, `PowerShell`, `Grep`, `Glob`,
  `WebSearch`, `WebFetch`, `Edit`, `Write` plus this project's equivalents
  (`read`, `bash`, `grep`, `find`, `edit`, `write`, `web_search`, `web_fetch`).
  The reference's `Glob` maps onto this project's `find`; `ls` stays out because
  the reference set does not contain a plain directory lister.
* placeholder: `[Old tool result content cleared]`
  (`TIME_BASED_MC_CLEARED_MESSAGE`).
* `keep_recent = 5` — the most recent compactable results always survive.
* count-based trigger (the cached-path rule): when more than
  `cached_trigger_threshold = 10` live compactable results exist, the oldest
  `active[:len - keep_recent]` are cleared, `getToolResultsToDelete` verbatim.
* time-based trigger: `gapThresholdMinutes = 60`, `keepRecent = 5`; the gap is
  measured from the last assistant message's timestamp to now, and the
  count-based rule is skipped when it fires (cold cache).
* token estimation (`estimate_message_tokens`, pure and unit-tested): text and
  thinking through `rough_token_count_estimation` (`round(len / 4)`, JS
  `Math.round` parity), a tool call through `name + JSON(input)`, an image or an
  image result block as `2000` (`IMAGE_MAX_TOKEN_SIZE`), and the total padded
  with `ceil(total * 4 / 3)`.

**Equivalence with `cachedMicrocompact`.** The reference's cached path does not
change the local messages at all: it emits a `cache_edits: [{type:
'delete_tool_result', tool_use_id}]` block so the provider deletes those results
server-side while the cached prefix stays byte-identical. Run Agent's extension
API has no cache-editing channel, so this port expresses the *same intent*
locally: the deletable set is still the oldest `len - keep_recent` results with
the same `TRIGGER_THRESHOLD = 10` / `KEEP_RECENT = 5` constants, and the
cacheable prefix (system prompt, a leading summary head, the first user message)
is never touched. A cleared result keeps its message and its `tool_call_id`, so
tool pairing is untouched.

### L2 — snip (`snip.py`)

* State is a boundary plus removals. The durable record is a `CustomEntry` in
  namespace `claude_compaction.snip` with the reference payload shape
  `{type: 'system', subtype: 'snip_boundary', content: '[snip] Conversation
  history before this point has been snipped.', isMeta: true,
  snipMetadata: {removedUuids: [...], trigger, reason, tokensFreed, createdAt}}`.
* The projection merges every boundary's `removedUuids` into one set and filters
  the view; the boundary itself is not a message here (see *Seam differences*),
  so the view receives the boundary *text* as one request-local user message
  placed right after the protected prefix. With no removals the input list is
  returned unchanged, which is what makes the projection idempotent. Removals
  are grown to whole tool-call pairs before they are applied (one assistant
  message plus every result that answers its calls, never a trailing user
  message), so a snip can neither orphan a tool result nor swallow the prompt a
  run is answering.
* `snip_compact_if_needed(messages, boundaries)` keeps the reference's
  "last boundary only" semantics and reports `tokensFreed =
  Σ max(1, ceil(chars / 4))`.
* `SNIP_NUDGE_THRESHOLD = 30`; at or above it the exact reference text
  (`SNIP_NUDGE_TEXT`) is appended to the request as a user message, and the
  `input` hook surfaces the same text to the user once per view size.
* Surfaces: the `snip` tool (mark by id/ordinal, by range, or everything older
  than the last N messages) and `/force-snip` (mark the whole current history).

### L3 — sessionMemoryCompact (`memory_compact.py`)

No model call at all: when the auto threshold is crossed, the existing memory
files are wrapped in the standard continuation header and reused as the summary.

* Files, in order: `~/.run/MEMORY.md`, `~/.run/USER.md`,
  `<cwd>/.run/MEMORY.md`, `<cwd>/.run/USER.md` — resolved through
  `RunAgentPaths.home` and `RunAgentPaths.project_run_agent_dir(cwd)`. These are
  the same paths the memory extension uses, because both follow
  `RunAgentPaths`; nothing else is shared.
* `DEFAULT_SM_COMPACT_CONFIG = {minTokens: 10_000, minTextBlockMessages: 5,
  maxTokens: 40_000}` and the reference's `calculateMessagesToKeepIndex` /
  `adjustIndexToPreserveAPIInvariants` decide how much history is kept verbatim.
* `getLastSummarizedMessageId` is ported as
  `SessionState.last_summarized_key` (a message key, see below). A key that is
  gone, an empty/missing memory file, or a post-compaction estimate that still
  crosses the auto threshold all return `None` so the caller **falls back to
  L4**.

### L4 — autoCompact / compact / reactiveCompact (`auto.py`)

* Window arithmetic: `effective = context_window -
  min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)` with
  `MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000`, buffer `13_000` (`30_000` at
  ≥400k effective, `50_000` at ≥800k), so `threshold = effective - buffer`.
  `COMPACTION_FOUR_LAYER_AUTOCOMPACT_PCT_OVERRIDE` (the
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` analogue) can only **lower** the threshold.
* Split point: `select_split_index` walks backwards to `keep_recent_tokens =
  20_000` and then `grouping.aligned_start_index` snaps it to an API-round
  boundary, so a tool call and its results always land on the same side. The
  index is clamped to leave at least one row to replace.
* Summary: `prompt.py`'s no-tools preamble + base compact prompt is sent through
  `services.inference.complete(...)` inside `asyncio.wait_for(...)` (the host
  request has no timeout of its own). The answer goes through
  `formatCompactSummary` (strip `<analysis>`, rewrite `<summary>` as
  `Summary:\n…`, collapse blank lines) and is wrapped by
  `getCompactUserSummaryMessage` — the reference continuation text with
  *"the summary below covers the earlier portion"*, the transcript path and
  *"Recent messages are preserved verbatim."*, plus the autonomous continuation
  sentence when the trigger is `auto`/`reactive`.
* A prompt-too-long failure of the summarizer itself is retried up to
  `MAX_PTL_RETRIES = 3` times with `truncateHeadForPTLRetry` (drop whole API
  rounds by token gap, else 20%, and re-assert a leading user message).
* Commit: `CompactionCommitRequest(summary, first_kept_entry_id, tokens_before,
  trigger, metadata)` is returned from `before_provider_request`; the core
  validates it and writes the `CompactionEntry` + `LeafEntry` pair. `trigger` is
  `auto` (threshold), `manual` (`/four-layer-compact`) or `reactive`.
  `metadata` carries the layer, replaced-row count, anchor key, model and
  snapshot id for the diagnostic log only.
* Circuit breaker: `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`. Three
  consecutive summary failures (empty answer, timeout, provider error) stop
  further L4 attempts for the session — the reference's fix for sessions that
  hammered the API with doomed compaction attempts. `InferenceBusy` /
  `InferenceUnavailable` are **not** failures: they defer, because the host
  refuses extension inference while a foreground run is in flight.
* Reactive path: armed by an observed provider error (HTTP `413` from
  `after_provider_response`, or an assistant error message that names a context
  overflow / oversized image/media), attempted **once per run** regardless of
  the threshold switch, and on failure only logged — it never touches the
  breaker and never sets a threshold latch. `state.reset_run()` at
  `before_agent_start` is what keeps it single-shot.

## Configuration

Environment variables, all optional (`config.py`, style follows
`run_agent_extensions.experience.config`):

| Variable | Default | Effect |
| --- | --- | --- |
| `COMPACTION_FOUR_LAYER_ENABLED` | `true` | Master switch. |
| `COMPACTION_FOUR_LAYER_L1_ENABLED` … `_L4_ENABLED` | `true` | Per-layer switches. |
| `COMPACTION_FOUR_LAYER_REACTIVE_ENABLED` | `true` | Reactive path switch. |
| `COMPACTION_FOUR_LAYER_KEEP_RECENT` | `5` | L1 keep-recent. |
| `COMPACTION_FOUR_LAYER_CACHED_TRIGGER_THRESHOLD` | `10` | L1 count trigger. |
| `COMPACTION_FOUR_LAYER_TIME_BASED_ENABLED` / `_TIME_GAP_MINUTES` | `true` / `60` | Time-based trigger. |
| `COMPACTION_FOUR_LAYER_SNIP_NUDGE_THRESHOLD` / `_USER_NUDGE` | `30` / `true` | L2 nudge. |
| `COMPACTION_FOUR_LAYER_IMAGE_MAX_TOKENS` | `2000` | Image accounting. |
| `COMPACTION_FOUR_LAYER_CONTEXT_WINDOW` | `128000` | Window used for the arithmetic. |
| `COMPACTION_FOUR_LAYER_MAX_OUTPUT_TOKENS_FOR_SUMMARY` | `20000` | Reserved output. |
| `COMPACTION_FOUR_LAYER_KEEP_RECENT_TOKENS` | `20000` | L4 retained tail. |
| `COMPACTION_FOUR_LAYER_AUTOCOMPACT_BUFFER_TOKENS` | `13000` | Threshold buffer. |
| `COMPACTION_FOUR_LAYER_AUTOCOMPACT_PCT_OVERRIDE` | unset | Percentage override (lowers only). |
| `COMPACTION_FOUR_LAYER_MAX_CONSECUTIVE_FAILURES` | `3` | Breaker limit. |
| `COMPACTION_FOUR_LAYER_SUMMARY_TIMEOUT_SECONDS` | `60` | `asyncio.wait_for` bound. |
| `COMPACTION_FOUR_LAYER_INLINE_MODEL_ATTEMPT` | `true` | Allow the in-request summary attempt (see the seams). |
| `COMPACTION_FOUR_LAYER_SM_MIN_TOKENS` / `_SM_MIN_TEXT_BLOCK_MESSAGES` / `_SM_MAX_TOKENS` | `10000` / `5` / `40000` | L3 keep-index bounds. |

### Strategy gate

`compaction.strategy` is read from the session's settings
(`~/.run/settings.json` and `<cwd>/.run/settings.json`, via
`load_settings(context.paths, context.cwd)`; an unreadable file counts as "not
four-layer"). When the strategy is **not** `four-layer` this extension does not
rewrite a single request: all four layers and the reactive path are inert. The
`snip` tool, `/force-snip` and `/four-layer-compact` stay registered so the
surface does not change under a user's hands; each of them reports that the
strategy is off instead of pretending it worked.

### Failure semantics

* Nothing here can bypass the core: `require_hard_limit` still runs after this
  extension's rewrite and still raises `ContextBudgetExceeded` for an oversized
  view. When L3/L4 cannot produce a summary in time, the request is sent as the
  free layers left it, and the hard-window guard decides.
* A rejected or failed commit is reported on `session_compact_failed`
  (`from_extension=True`) and counts toward the breaker; an invalid request is
  ignored by the core with a diagnostic and never half-committed.
* Persistence failures (a `CustomEntry` that cannot be appended) degrade to
  session-local state and are reported in the surface's text.

## Seam differences from the reference

| Reference | This port | Why |
| --- | --- | --- |
| Boundaries are `system`/`snip_boundary` messages inside the transcript. | Boundaries are `CustomEntry` records in `claude_compaction.snip`; the view receives the boundary text as a request-local user message. | The extension API has no way to append a transcript message, and a provider view accepts only user/assistant/toolResult roles. `api.append_entry` is the documented durable channel for extension state. |
| Messages carry `uuid`s used by `removedUuids`. | `message_key(message)` — a digest of the serialized message — stands in. | Run Agent messages have no id. A message converted on the way into the view (custom/bash/summary message → user message) has a different digest, so `/force-snip` reissues the marking rather than relying on old keys. |
| `cachedMicrocompact` deletes via `cache_edits`. | Content clearing outside the cacheable prefix. | No cache-editing channel; see the L1 equivalence note. |
| `ensureToolResultPairing` repairs splits at API time. | `state.legalize_view` re-applies the core's `repair_tool_history` after every layer, and L2 grows removals to whole tool-call pairs first. | The core repairs the view *before* `before_provider_request`, so this port must hand back an already legal view. |
| The summary runs as a forked agent with the parent's cache and tool set. | One bounded `services.inference.complete` request. | The host owns the provider; an extension cannot fork the transcript. |
| Session memory is a live extractor (`SessionMemory/*`). | L3 reads the existing `MEMORY.md` / `USER.md` files by path. | The memory extension owns extraction and writes; see *Boundaries*. |
| Snip projection keeps the boundary message in the view. | No boundary message is injected; the text is injected once by the projection. | Same reason as the first row. |
| Time-based L1 may clear results inside the old prefix (cold cache). | The protected prefix is never cleared, even when the cache is cold. | The project's prefix-stability contract is stricter than the reference's cache model. |

Not implemented, deliberately: `postCompactCleanup.ts` (its caches belong to the
reference's process — nothing equivalent is shared here), the post-compact file /
skill / plan attachment restoration, `compactWarning*` GrowthBook plumbing, the
`up_to` partial-compaction direction, and the reference's `MAX_OUTPUT_TOKENS_*`
provider quirks. `automatic compaction from the core` is off by design under
`four-layer`, so no double compaction can occur.

### Seam limits worth knowing

* **Inference during a run.** `before_provider_request` runs inside a foreground
  run, and the host refuses extension inference then (`InferenceBusy`). The
  model summary is therefore normally prepared at `agent_settled` (idle) or by
  `/four-layer-compact`, and the in-request attempt simply defers. That is why
  L3 (no model call) is tried first, and why the breaker ignores the deferral.
* **Entry ids.** `CompactionCommitRequest.first_kept_entry_id` must name an
  active durable entry, but no extension-facing API lists them. This port reads
  `context_entry_ids` out of the payload of the last recorded agent snapshot
  (`context.current_snapshot_id`, or the settling run's `snapshot_id`). The id
  list can be one turn stale; the index is clamped so the boundary always errs
  towards keeping *more* history than the summary covers, never less. When no
  snapshot has been seen yet, the view rewrite still happens and the commit is
  skipped with a diagnostic (the session keeps working; the next request has a
  snapshot to read).
* **Resume.** Boundary `CustomEntry` records are durable, but they cannot be
  enumerated after a restart, so a resumed session starts with an empty
  projection and the boundaries appended in this process. Re-run `/force-snip`
  after a resume if the previous marks must apply again.

## Boundaries with the memory extension

This package **never imports** `run_agent_extensions.hermes_memory` (or
`experience`). Package-to-package imports would couple two independently loaded
extensions: either could be disabled, reloaded or replaced, and the compaction
path must not break with it. L3 therefore reads the memory files by path using
`RunAgentPaths`, treats them as read-only input, and does not reuse hermes'
snapshot, threat-scanning or write-approval machinery. Write ownership stays with
the memory extension; this package never writes to `MEMORY.md` or `USER.md`.

`claude_compaction` is also independent of the core's `cheap-first` strategy: the
two are mutually exclusive by `compaction.strategy`. Under `cheap-first` the core
runs its own free L3/L1/L2 rewrites and only then considers its own summarization;
this extension stays inert, which is why its configuration is not consulted in
that mode.

## Testing

`tests/redesign/test_claude_compaction_*.py` cover the pure layers, the state
round-trip, the window arithmetic, the breaker and the reactive latch, plus an
end-to-end run on the real `CodingApplication` with a fake provider asserting
that the provider actually received a four-layer-processed request while the
session JSONL history stayed untouched. Everything is offline: no test performs a
real model call.
