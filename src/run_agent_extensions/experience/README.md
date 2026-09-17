# Experience

Built-in Session extension that gives the agent long-term memory and a way to grow
and maintain its own Skills. New CLI and Gateway sessions load it by default through
CodingApplication; Core remains unchanged.

```powershell
run
run gateway --cwd .
# Opt out of default/discovered extensions (explicit --extension paths still load):
run --no-extensions
run gateway --no-extensions
```

The design follows hermes-agent's memory tool, skill manager, background review and
curator. Memory is two small Markdown files with a character budget, pasted into the system
prompt once per session. Skills are directories the model creates and patches in the
foreground, that a background review extends after a finished run, and that a curator ages,
archives and consolidates over weeks. Mutations are validated, counted and recorded through
the experience write paths so the user can see what changed and undo one edit at a time;
Skill content scanning is enabled separately by `EXPERIENCE_SKILL_GUARD`.

Existing sessions preserve their saved extension snapshot. Adopt experience while keeping
history with `run --session <id> --refresh-resources`, or restart the Gateway with
`run gateway --cwd . --refresh-resources`. Project memory and Skills remain subject to the
existing trust policy; pass `--trust-project` only for a project you trust. Repeating
`--extension experience` does not load a second copy. Automatic review and lifecycle
maintenance are enabled by their existing defaults; consolidation remains opt-in.

## Memory files

| File | Holds | Default scope | Budget |
| --- | --- | --- | --- |
| `USER.md` | who the user is, how they want the agent to work | user (`~/.run/USER.md`) | 1375 chars |
| `MEMORY.md` | durable facts about the project and its environment | project (`<cwd>/.run/MEMORY.md`) | 2200 chars |

Both scopes exist for both files; the tool and command pick the default scope from the
target and accept `scope` to override it. Entries are separated by `\n§\n` and each may span
several lines of Markdown.

The prompt shows a snapshot captured at session start or `/reload`, rendered with a usage
header (`MEMORY (your personal notes) [45% — 990/2,200 chars]`). A write during the session
lands on disk immediately but does not move the prompt, so the provider's prefix cache
survives. The next session, or `/reload`, picks the new entries up.

What keeps the files trustworthy:

- Every write is scanned against the threat pattern library (`threats.py`: prompt injection,
  role hijack, command-and-control vocabulary, secret exfiltration, backdoors, invisible
  Unicode, in English and Chinese, with NFKC folding). A match refuses the write. At snapshot
  time a poisoned entry that reached the file some other way is replaced in the prompt by a
  `[BLOCKED: ...]` marker while the raw entry stays for the user to inspect and remove.
- A mutation takes a sidecar lock and re-reads the file first, so a second session or a hand
  edit is never overwritten from a stale view. Content that would not round-trip through the
  entry format (a shell append, a patch) is backed up to `MEMORY.md.bak.<ts>` and the write
  is refused. An unreadable file is never rewritten from an assumed-empty view.
- A write that would exceed the budget is refused with the current entries, so the model
  consolidates with `replace` or `remove`. `batch` applies several operations against the
  final budget in one call, all-or-nothing. Three at-capacity failures in one turn become a
  terminal "stop retrying" answer so memory can never loop a turn to exhaustion.
- A duplicate `add` is idempotent, `replace` and `remove` refuse an ambiguous substring, and
  a success answer is final: it does not echo the entries, so the model does not go looking
  for more to fix.

Project-scope files share the project trust gate with every other project input: in an
untrusted working directory they are neither shown nor writable.

## Skills

A managed Skill is an ordinary `<skills-dir>/<name>/SKILL.md` directory under the user or
project skills directory, so the regular loader picks it up and freezes it on the next
session or `/reload`. Support files go under `references/`, `templates/`, `scripts/` or
`assets/`. The frontmatter records `created_by: agent` or `created_by: review`.

Next to the skills live three sidecars the tool maintains:

| File | Purpose |
| --- | --- |
| `.usage.json` | Per-skill telemetry and lifecycle: view, use and patch counts, patch generation and reuse-after-patch, `state` (active, stale, archived), `pinned`, and `created_by: agent` which marks a skill as curator-managed. |
| `.ledger.jsonl` + `.blobs/` | Append-only audit trail of every mutation by every actor (`agent`, `review`, `curator`, `user`) with before/after manifests; file contents are stored content-addressed. `/curator rollback <id>` restores one edit, after capturing a safety entry so the rollback is itself undoable. |
| `.archive/` | Where the review and the curator move skills instead of deleting them; `/curator restore <name>` brings one back. |

Every write goes through the same validation, ownership and ledger paths. When
`EXPERIENCE_SKILL_GUARD=true`, the resulting Skill directory is also scanned for threats,
symlinks escaping the directory, binaries and oversized files; a dangerous verdict rolls
the write back. Advisory lint is returned with the result and never blocking: marketing
words, a missing "When to Use" section, shell utilities named in prose instead of native
tools, dangling `references/` links, scaffolding files.
- Ownership: a skill is user-owned unless the review created it or the user ran
  `/curator adopt <name>`. Autonomous writers (the review, the curator) refuse user-owned
  and pinned skills, must have viewed a file in the same pass before patching it, and may
  archive only while naming the umbrella that absorbed the content.

## Tools and commands

| Surface | Purpose |
| --- | --- |
| `memory` tool | `add`, `replace`, `remove` or `batch` (a list of those) on `USER.md` or `MEMORY.md`. |
| `skill_manage` tool | `list`, `view`, `create`, `edit`, `patch`, `write_file`, `remove_file`, `delete` (with `absorbed_into`). |
| `/memory show\|add\|replace\|remove ...` | Read or edit memory by hand. |
| `/review now [focus]`, `/review status` | Run the background review on demand, or see the last outcome. |
| `/curator status\|run\|dry-run\|pause\|resume` | Inspect or drive the curator. |
| `/curator pin\|unpin\|adopt\|restore\|ledger\|rollback\|archived` | Pin, adopt, restore and roll back the Skill library. |

`/skill:<name>` expands the selected Skill into the prompt and counts one use in its
source library; a shadowed user Skill is not counted when the project Skill was selected.
Successful `read` calls on the selected Skill's `SKILL.md`, including its frozen cache
copy, also count consultations. Views count successful reads; uses are deduplicated per
Skill per user run. Failed reads, review/curator work and evaluation runs do not contribute.
An old frozen package does not earn reuse credit for a newer patch. Shell commands and
arbitrary custom readers are not inferred as Skill use.

Identical Skill edits and support-file writes return `changed=false` without new patch
counts, audit entries or learning notifications. Body edits preserve unrelated frontmatter;
explicit document fields replace their corresponding top-level blocks while identity and
creation provenance stay fixed. Skill files are published with an atomic replacement that
preserves existing file modes. This is a single-file guarantee, not a multi-file transaction.
Restoring a Skill starts a fresh activity window without inventing additional uses.

## Background review

Automatic review is enabled, but does not run after every task. Admission happens only
on `agent_settled`, after the final history and outcome commit; ordinary tool events must
not consume the pending review flags. The current triggers are:

- Memory cadence: 10 user inputs (`EXPERIENCE_MEMORY_NUDGE_INTERVAL`; the older
  `EXPERIENCE_REVIEW_EVERY_TURNS` name remains an alias).
- Skill cadence: 10 model rounds (`turn_start` / `EXPERIENCE_SKILL_NUDGE_INTERVAL`),
  including the final no-tool round. Accepted `memory` / `skill_manage` calls reset
  their respective counters; a due memory flag stays latched until settlement.
- With `EXPERIENCE_REVIEW_ON_SIGNALS=true` (off by default), a recognized user
  correction or a non-successful settled run admits review. Recovered tool errors and
  failed tests are not separately counted, and external benchmark grading is not fed
  back into this trigger.
- Fallback admission after 10 settled runs. One-turn chitchat is skipped unless a
  cadence nudge or opt-in signal admits it.

The cooldown defaults to zero; cadence nudges bypass it. Auxiliary review/evaluation origins
are excluded. A review already in flight prevents another one from being admitted.
The local regression test covers 9 model rounds without review and 10 rounds with review and
a Skill write; this verifies wiring, not improved performance on future tasks.

1. The completion records one durable `review-request:<run_id>` in session state and submits
   `experience-review` as a managed task under the `review` origin kind, so a review can never
   trigger another review.
2. The task waits a bounded time for the foreground to go idle, then claims the single outcome
   for that run before spending anything.
3. It shows the model the completed run's committed entries from a fixed host history source
   and verified input snapshot metadata. Current saved memory and the Skill index are included
   as data so the review can distinguish a new lesson from one already saved. An edit of an existing Skill must first view its target file in the
   same pass. Manual `/review now` uses the current transcript because it has no completed-run
   source.
4. The model receives only the native `memory` and `skill_manage` tool definitions. The
   host returns proposed calls without executing them; the review applies the same guards
   to every call. A bounded consecutive JSON-call format remains supported for text-only
   providers. The answer is a bounded sequence of memory and Skill tool calls (`create`, `patch`,
   `write_file`, `remove_file`, or a named `delete`). Each is applied within an edit budget
   through the same guards as a foreground write; a refused edit is recorded as skipped rather
   than retried.
5. Applied changes produce a notification; `EXPERIENCE_REVIEW_NOTIFY=verbose` includes
   change previews and `off` suppresses it. A review may finish with nothing to save.

The native/JSON tool loop defaults to at most 16 model requests and 16 tool calls, with
a cumulative 600,000 input-token budget. Budget preflight uses a character estimate and
reported input usage when available. New user input requests cancellation and waits up to
2 seconds for acknowledgement before proceeding; cancellation checks prevent late writes.
Remote computation may continue after the local request is cancelled.

The older `{"memory": [...], "skills": [...]}` batch format still has a compatibility
path through the same write guards. Its items are not counted by the native tool-loop
ledger, so the 16-tool limit does not cover that legacy batch path.

The review prompt carries hermes' rules: prefer patching a loaded skill, then a support file,
then a new class-level umbrella; embed user-preference corrections in the governing skill and
not only in memory; never capture environment failures, negative claims about tools,
transient errors or unresolved attempts.

## Curator

The curator maintains the skills the agent created, on an interval when the session has been
idle (`EXPERIENCE_CURATOR_INTERVAL_HOURS`, default 168 hours;
`EXPERIENCE_CURATOR_MIN_IDLE_HOURS`, default 2 hours).
Two passes:

- Automatic transitions still need the Skill mutation gate. From each managed Skill's latest
  activity the state moves active → stale → archived. A pinned Skill is never touched;
  stale and archive thresholds default to 30 and 90 days. Archiving is recoverable, and
  `EXPERIENCE_SKILLS_WRITE_APPROVAL=true` blocks the autonomous
  mutating pass because it has no interactive approval UI.
- Consolidation asks the model for an umbrella-building plan and is off by default
  (`EXPERIENCE_CURATOR_CONSOLIDATE=true`, or `/curator run`). The plan may patch or create
  class-level Skills, demote narrow siblings into support files, and archive what was absorbed;
  every archive must name its umbrella, and every step runs through the review guards under the
  `curator` actor in the ledger.

Each run writes a report under `~/.run/experience/.curator_reports/` and updates
`.curator_state.json`; `/curator dry-run` previews without changing anything. A host-owned maintenance registry ticks the curator during housekeeping for cached agents,
independent of whether that agent currently has a foreground turn. Foreground, review and curator Skill mutations take a per-root writer lease, while a curator lease serializes whole-library passes.
Curator mutations can take a whole-library backup (`EXPERIENCE_CURATOR_BACKUP`, with the
retention count controlled by `EXPERIENCE_CURATOR_BACKUP_KEEP`) before applying changes.

## Configuration

All from the environment: `EXPERIENCE_MEMORY_CHAR_LIMIT`, `EXPERIENCE_USER_CHAR_LIMIT`,
`EXPERIENCE_MEMORY_ENABLED`, `EXPERIENCE_USER_PROFILE_ENABLED`, `EXPERIENCE_REVIEW_ENABLED`,
`EXPERIENCE_MEMORY_NUDGE_INTERVAL`, `EXPERIENCE_SKILL_NUDGE_INTERVAL`,
`EXPERIENCE_REVIEW_EVERY_TURNS` (legacy alias), `EXPERIENCE_REVIEW_COOLDOWN_SECONDS`,
`EXPERIENCE_REVIEW_NOTIFY`, `EXPERIENCE_REVIEW_MAX_ITERATIONS`,
`EXPERIENCE_REVIEW_MAX_INPUT_TOKENS`, `EXPERIENCE_REVIEW_CANCEL_TIMEOUT_SECONDS`,
`EXPERIENCE_REVIEW_ON_SIGNALS`, `EXPERIENCE_CURATOR_*`, `EXPERIENCE_MEMORY_WRITE_APPROVAL`,
`EXPERIENCE_SKILLS_WRITE_APPROVAL`, `EXPERIENCE_SKILL_GUARD` (default false),
`EXPERIENCE_SKILL_LEDGER` (default true). Ownership and mutation permission checks remain
active when optional Skill content scanning is off.

Review completions default to `EXPERIENCE_REVIEW_THINKING=off` and
`EXPERIENCE_REVIEW_MAX_OUTPUT_TOKENS=1600` (per model request). These are independent
of the main task's reasoning level and do not alter its active provider or context.
The host creates and closes a temporary configured adapter, including the usual
provider instrumentation. Custom injected SDK providers and dynamic providers own
their generation policy; the host keeps using them and records `policy=provider_owned`
with `applied=null` in the input snapshot. Configured adapters record both requested
and applied controls; endpoints remain responsible for honoring those parameters.

## Boundaries

- The extension can be disabled with `--no-extensions`; explicit extension paths still load.
- Memory context is data, not permission: it cannot grant tool approvals, and a current explicit
  user instruction takes precedence over anything remembered.
- Hosts that mark a run with the evaluation origin disable learning writeback for that run.
  An external benchmark runner using ordinary sessions must isolate trial memory/Skills
  itself and record whether review was enabled; benchmark labels alone do not disable writes.
- Nothing autonomous ever hard-deletes: the review and the curator archive, and the ledger can
  restore any single edit. A foreground delete on the user's request is a real delete, still
  ledgered.
