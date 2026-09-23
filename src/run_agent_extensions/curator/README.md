# Curator

The Curator is the maintenance half of the Skill system: it keeps the library clean over
time without ever editing a Skill body. It is a port of hermes' skill curation (the
inactivity transitions, the whole-library snapshots with rollback, the bounded model
review and the unified journey view) re-shaped so that **every content change still has to
pass the existing gate**:

```text
Curator review  ->  SkillEvolution.propose(...)  ->  candidate (cold)
                ->  host EvaluationReport (passed)
                ->  user runs /evolve publish    ->  formal Skill body
```

`run_agent_extensions/curator` never calls `SkillCandidateStore.create` and never writes a
`SKILL.md`. Its only direct writes are:

* moving a whole Skill directory into `<skills-root>/.archive/` (archive) and back
  (restore), both under `SkillManager.write_scope` and both recorded in the existing Skill
  ledger;
* its own state (`state.json`, `usage.jsonl`, `reports/`, `backups/`) under
  `<paths.extension_state_dir>/curator/`.

## Four responsibilities

### 1. Curation transitions (no model)

`apply_automatic_transitions(records, *, now, config, library)` moves each discovered
Skill between `active`, `stale` and `archived` from one activity anchor:

```text
anchor = last_consulted_at or last_mutation_at or mtime
```

* `last_consulted_at` comes from this extension's `usage.jsonl`, bounded by
  `CURATOR_USAGE_LOOKBACK_DAYS` so only a *recent* consultation counts;
* `last_mutation_at` is the newest Skill-ledger entry for that name **excluding
  `archive`** (being moved away is not activity) — `restore`, `publish`, `adopt` and
  `rollback` all count;
* `mtime` is the `SKILL.md` file time.

Rules with `stale_after_days` (`S`) and `archive_after_days` (`A`), defaulting to 30 and 90:

| Condition | Action |
| --- | --- |
| `anchor <= now - A` and the Skill is archivable | archive the whole directory |
| `anchor <= now - S` and state is `active` | mark `stale` |
| `anchor > now - S` and state is `stale` | mark `active` (reactivate) |
| otherwise | nothing |

A Skill that is *discovered* is never archived: a stored `archived` record for a Skill
that is present again (restored, re-created) is normalized to `active` before the rules
run, so the pass is idempotent and recomputable offline.

**Protection, in priority order** (`CuratorLibrary.protected`):

| Reason returned | Default | Meaning |
| --- | --- | --- |
| `pinned` | legacy `.usage.json` | never auto-archived, and every automatic transition is skipped |
| `protected name` | `CURATOR_PROTECTED_SKILL_NAMES` (empty) | same as pinned |
| `project inputs are untrusted` | untrusted session | the project scope is not read at all |
| `project scope is not auto-archived` | `CURATOR_AUTO_ARCHIVE_PROJECT_SKILLS=false` | project Skills may be marked `stale`, never archived |
| `user-written skill is not auto-archived` | `CURATOR_AUTO_ARCHIVE_USER_SKILLS=false` | anything whose frontmatter is missing `created_by` or does not say `evolution` |

So the default is *default-deny*: only an unprotected, unpinned, user-scope Skill with
`created_by: evolution` can be archived automatically. Every refusal is reported in
`skipped` with its reason and never silently dropped. `/curator run --dry-run` runs the
same rules with `apply=False` and reports what *would* happen.

### 2. Snapshot and rollback

`snapshot_skills(root, *, reason, state_dir)` writes
`<state.extension_state_dir>/curator/backups/<utc-id>/skills.tar.gz` plus `manifest.json`
(`id`, `reason`, `created_at`, `archive`, `archive_bytes`, `skill_files`, `root`) before
any mutating pass, so the file needed to undo the pass exists before the pass starts.
The archive includes every top-level entry of the root **except** the Curator's own
bookkeeping (`.curator_backups`, `.write.lock`); `.ledger.jsonl`, the ledger blobs and
`.archive/` are included so a rollback also restores the audit trail and the archived
Skills. `prune_old(state_dir, keep=..., protect=...)` keeps the newest `keep` snapshots
(`CURATOR_BACKUP_KEEP`, default 5) and never removes a protected id.

`restore(snapshot_id, *, root, state_dir, lock=...)` is a transaction:

1. snapshot the current tree first (`reason="pre-restore to <id>"`, protecting the target
   id so pruning cannot evict the snapshot being restored);
2. move every current top-level entry into `<root>/.rollback-staging-<id>`;
3. validate every tar member — absolute paths, `..`, symlinks, hardlinks and device nodes
   are refused before anything is extracted;
4. extract into the now-empty root;
5. on failure, delete what the extract created, move the staged entries back, and drop the
   staging directory — if the move back also fails, keep the staging directory and say so
   in the message instead of claiming a clean restore;
6. on success, drop the staging directory and report the pre-restore snapshot that holds
   the replaced tree (including its `.archive/` and ledger).

The whole sequence runs inside the lock the caller passes (the extension passes
`SkillManager.write_scope`).

### 3. Journey

`journey.py` is one read-mostly view over Skills and memory:

| Node id | Source |
| --- | --- |
| `<scope>/<name>` (`user/deploy`, `project/deploy`) | a discovered Skill |
| `memory:memory:<index>` | a non-empty `§`-delimited block of `MEMORY.md` |
| `memory:profile:<index>` | the same for `USER.md` |

The index is the *global* position of a non-empty block across the ordered files
(`MEMORY.md` user scope, then project scope, then `USER.md`), and each entry keeps its
own file and local index, so empty blocks are skipped without shifting the ids.

`list` and `show` are read-only. `delete <id>` follows the ownership split: a Skill is
**archived** (never deleted; a pinned Skill is refused), and a memory is **refused** with
a pointer to `/memory`, because memory writes belong to the memory extension.

### 4. Review → candidates

`run_review` sends **one** bounded completion request (no agent fork, no tool loop) with a
hard `max_output_tokens` ceiling and an `asyncio.wait_for` deadline. The model sees a
metadata-only candidate list (name, scope, `created_by`, state, pinned, last consulted,
last mutation, first 12 digest characters) and may only answer with names and reasons:

```yaml
consolidations:
  - from: <old-skill-name>
    into: <umbrella-skill-name>
    reason: <one short sentence>
prunings:
  - name: <skill-name>
    reason: <one short sentence>
```

It is never asked for file content, so nothing it says can become a Skill body by itself.
The Curator reconciles the plan with deterministic evidence and builds the composition:

**Reconciliation priority** (highest first)

1. a `from` that is not a live Skill, or that names itself, is dropped with a reason;
2. a live Skill whose body mentions the source name (whole-word, `-`/`_` interchangeable)
   wins over the model's guess: the declared `into` is kept when it is one of those Skills
   (`evidence="absorbed"`), otherwise the alphabetically first mention is used
   (`evidence="absorbed-elsewhere"`);
3. with no body evidence at all, the declared `into` is kept when it exists
   (`evidence="unverified"`);
4. otherwise the entry is dropped with a reason (the review never invents a Skill);
5. a name any consolidation mentioned is never pruned in the same pass.

**Composition rule**: the destination Skill gains exactly one `add` operation appending

```markdown
## Absorbed: <source>

<bounded excerpt of the source body, frontmatter removed, <= 1200 characters>
```

which keeps the candidate inside the existing `MAX_PROPOSER_OPERATIONS=8` /
2,000-changed-character budgets and cannot touch the frontmatter of an existing Skill.

`apply_review` then:

* calls `SkillEvolution.propose(scope=into.scope, name=into, source_session=...,
  source_run=state.last_review_run_id, operations=...)` for each consolidation, so
  ownership, pin, scope, base digest, lint, security scan, operation and size limits are
  enforced by the one place that already enforces them. A refusal is reported as
  `skipped: candidate refused: ...`;
* archives each pruning through the same protected move the automatic transitions use
  (`archived: true`, ledger id recorded) and reports `archived: false` for a dry report.

`llm_summary`, `llm_error` and `skipped` carry every degradation: an unavailable provider,
`InferenceBusy`, a timeout, an unparsable answer or a refused proposal. **No review
failure changes a single Skill file.** The per-run `reports/<run-id>/run.json` (fields
`started_at`, `duration_seconds`, `model`, `provider`, `auto_transitions`, `counts`,
`archived`, `consolidated`, `pruned`, `candidates`, `state_transitions`, `llm_summary`,
`llm_error`) and `REPORT.md` say so explicitly: the report describes library hygiene, not
a capability improvement.

## State layout and configuration

```text
<paths.extension_state_dir>/curator/
    state.json    last_run_at, last_run_duration_seconds, last_run_summary,
                  last_run_summary_shown_at, last_report_path, paused, run_count,
                  last_review_run_id, last_review_run_at, records{<scope>/<name>:{state,since}}
    usage.jsonl   append-only {skill,scope,consulted_at,source}; source is one of
                  slash_command, skill_tool, turn
    reports/<id>/ run.json + REPORT.md
    backups/<id>/ skills.tar.gz + manifest.json
```

Every write is atomic (temp file, `fsync`, `os.replace`) and takes one advisory file lock
(`.state.lock`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `CURATOR_ENABLED` | `true` | master switch |
| `CURATOR_INTERVAL_HOURS` | `168` | minimum age of `last_run_at` before a background pass |
| `CURATOR_STALE_AFTER_DAYS` | `30` | inactivity that marks a Skill stale |
| `CURATOR_ARCHIVE_AFTER_DAYS` | `90` | inactivity that archives an unprotected Skill |
| `CURATOR_AUTO_ARCHIVE_USER_SKILLS` | `false` | allow automatic archiving of user-written Skills |
| `CURATOR_AUTO_ARCHIVE_PROJECT_SKILLS` | `false` | allow automatic archiving of project-scope Skills |
| `CURATOR_BACKUP_ENABLED` | `true` | take pre-run snapshots |
| `CURATOR_BACKUP_KEEP` | `5` | snapshots kept per Skill root |
| `CURATOR_LLM_REVIEW_ENABLED` | `true` | run the bounded review |
| `CURATOR_LLM_REVIEW_MAX_OUTPUT_TOKENS` | `1200` | review output ceiling |
| `CURATOR_LLM_REVIEW_TIMEOUT_SECONDS` | `60` | review deadline |
| `CURATOR_USAGE_LOOKBACK_DAYS` | `30` | how recent a consultation must be to count |
| `CURATOR_PROTECTED_SKILL_NAMES` | empty | comma-separated names that never move automatically |

## Commands

| Command | Notes |
| --- | --- |
| `/curator status` | read-only: policy, counters, per-Skill states, protected names, what would change now, snapshots |
| `/curator run [--dry-run]` | full pass; `--dry-run` is read-only and does not move the cadence clock |
| `/curator pause` / `/curator resume` | confirm first |
| `/curator restore <snapshot-id\|archived-skill>` | confirm first; a bare name is resolved in the project then user archive |
| `/curator report [run-id]` | read-only; the newest report when no id is given |
| `/curator review` | confirm first; the review half only (no transitions, no clock movement) |
| `/curator learn <description> [--name <skill>] [--scope user\|project] [--run <id>]` | `SkillEvolution.propose_from_run` against `state.last_review_run_id`, refusing clearly when no completed run is recorded |
| `/curator journey list\|show <id>\|delete <id>` | `delete` confirms first; deleting a memory is always refused |

Every destructive action (`run`, `pause`, `review`, `restore`, `journey delete`) asks
`context.ui.confirm` first. With no UI the confirm returns `False`, so a non-interactive
session can only run `/curator status`, `/curator report` and `/curator run --dry-run`.

## Hooks

* `session_start` — the cadence gate: `enabled`, not `paused`, a recorded `last_run_at`
  and `now - last_run_at >= interval_hours`. The first start only seeds `last_run_at` and
  reports "deferred first run" (no snapshot, no transitions). A due run snapshots every
  eligible Skill root (a failed snapshot is reported, never fatal), applies the
  transitions, runs the review when it is enabled and inference is available, writes the
  report and stamps the state.
* `agent_settled` — records `last_review_run_id`/`last_review_run_at` only; it never calls
  inference.
* `input` / `tool_call` — record `/skill:<name>` and `skill_manage(action=view)`
  consultations in `usage.jsonl`.
* `session_shutdown` — flushes the state document.

Every hook body is wrapped: a failure is logged, appended to the session diagnostics
(visible in `/curator status`) and never propagates into the session. All writes are
idempotent.

## Differences from hermes

| hermes | here | Why |
| --- | --- | --- |
| the curator writes Skill bodies/support files directly through `skill_manage` and `terminal` | content changes are `SkillEvolution.propose` candidates only | the project's one gate: ownership, pin, scope, base digest, lint, scan, host evaluation, `/evolve publish` |
| a forked `AIAgent` with `max_iterations=9999`, `enabled_toolsets=["skills","terminal"]` | one `InferenceService.complete` request with an output ceiling and a deadline | no second agent, no tool loop, no unbounded spend |
| the model may create a new umbrella Skill (`skill_manage action=create`) | the review may only append `## Absorbed: <name>` to an *existing* live Skill | a new Skill is a user/experience decision, not a review side effect |
| consolidating archives the absorbed siblings immediately | a skill named in a consolidation is **never** pruned in the same pass | the absorption is only a candidate; the source content stays live until it is published |
| `absorbed_into=` on the delete tool call is the authoritative classification | deterministic body evidence (`word-boundary search in another Skill's body`) + the model's `from/into` as a fallback | there is no delete tool call to inspect, and content evidence is reproducible |
| `curator.json` config file (`curator.*` keys) | `CURATOR_*` environment variables read from the extension context | matches `experience/config.py` and the other built-ins |
| ledger actor tagged `"curator"` via a context variable | ledger actor `"evolution"` (reason, scope, root and digest in `evidence`) | `skill_ledger.VALID_ACTORS` is `{agent, user, evolution}`; the mapping is this line |
| `.archive/<name>[-<timestamp>]` with a `mv` from the shell | the same move performed in-process under `SkillManager.write_scope`, with `capture_before` + `record` | one lock owner, one audit trail, no shell |
| `.usage.json` is the curator's own state | pinned is still read from it, but state/usage live in `extension_state_dir/curator/` | the legacy sidecar stays read-only |
| no lock during rollback; `.curator_backups` lives inside the skills root | restore runs under `write_scope`, backups live in the extension state dir (so a snapshot cannot recurse) | cross-process safety, no self-capture |
| the manifest has no root | `manifest.json` carries `root` | `restore` must know which Skill root a snapshot belongs to |
| snapshot excludes `.hub/` | no hub concept; only `.curator_backups` and `.write.lock` are excluded, `.ledger.jsonl`/`.blobs`/`.archive` are included | a rollback should undo the ledger and the archives too |
| a failed snapshot aborts the pass | a failed snapshot is recorded as a diagnostic and the pass continues | a disk hiccup must not silently disable maintenance |
| `journey delete` rewrites or deletes a memory file | deleting a memory is refused with a pointer to `/memory` | memory ownership belongs to the memory extension |
| project/bundled skills are curated under flags | project scope is stale-marked only; `created_by` decides the user case | default-deny for anything the user wrote |
| the review runs unconditionally on a consolidated list | the review is bounded, reconciling, and every failure leaves the library untouched | failure semantics below |

The Curator builds its **own** `SkillEvolution` over the same stores the experience
extension uses, so a candidate proposed here appears in `/evolve candidates` (the store is
a path, not an object). It does not call the experience extension's instance and does not
import its state.

## Failure semantics

* A snapshot failure never blocks a pass; it is recorded in `state.last_run_summary`,
  `run.json.snapshot_error` and `REPORT.md`.
* An unavailable or busy inference service, a timeout, an unparsable answer or an empty
  answer set `llm_error`/`llm_summary` and leave every Skill untouched.
* A refused candidate (`user-owned`, pinned, untrusted project, drifted digest, size or
  scan limits) is reported in `skipped`, and nothing is written.
* A refused archive (protected record, untrusted project, missing directory) is reported
  in `skipped`; the Skill stays in place.
* A failed restore puts the previous tree back; if that also fails, the staging directory
  is kept and named in the message.
* `state.json`, `usage.jsonl` and reports swallow their own I/O errors: telemetry failures
  degrade the record, never the session.
* The actor mapping for ledger entries is `"evolution"` for both archive and restore,
  because `skill_ledger.VALID_ACTORS` accepts only `{agent, user, evolution}`; the entry's
  `evidence` holds the reason, scope, source path, root and the archived digest.

## Relationship to the experience gate

`experience` remains the only way a Skill body changes. The Curator adds maintenance
around it: it may mark unused Skills stale and archive unprotected evolution-owned Skills
into `.archive/`, it may propose consolidation candidates through the gate, and it reports
what it did. Publishing, rejecting, adopting and rolling back a candidate stay user
commands under `/evolve`, and the evaluation service still decides whether a candidate is
`verified` or `rejected`.
