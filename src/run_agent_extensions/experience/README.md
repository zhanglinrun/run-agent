# Experience

The built-in Experience extension provides verifier-gated Skill evolution. Memory lives in
the separate built-in `memory` extension
([`run_agent_extensions/hermes_memory`](../hermes_memory/README.md)), which owns
`USER.md` / `MEMORY.md`, the `memory` tool and the `/memory` command. It does not run a
background review, cadence trigger, curator, or automatic maintenance loop.

Existing sessions preserve their recorded extension snapshot. Use
`run --session <id> --refresh-resources` to adopt the current extension implementation while
keeping history. Project Skills and project probes remain subject to the existing project
trust policy.

## Published Skills

Published Skills remain ordinary `<skills-root>/<name>/SKILL.md` packages. The standard
Skill loader discovers them, freezes their package contents in the session resource snapshot,
and applies normal user/project precedence. The Experience extension does not replace that
loader.

The foreground `skill_manage` tool only exposes:

- `list`: list published Skills.
- `view`: read a file in one published Skill package.
- `propose`: materialize an immutable candidate from at most 8 add/delete/replace operations
  with at most 2,000 changed characters total.

The tool has no create, edit, patch, delete, support-file write, publish, ledger, or rollback
action. Formal Skill changes are user commands under `/evolve` and cannot be invoked through
the model tool schema.

## Candidate Store

Candidates are stored outside all Skill loader roots under
`~/.run/experience/candidates/`:

```text
candidates.jsonl
blobs/<sha256>.md
```

`candidates.jsonl` is append-only. Schema `run-agent.skill-candidate.v1` records the candidate
ID, scope/name, host-bound source session and committed run, base and candidate digests,
operations, claims, probe digests, evaluation report ID, and status events. Materialized
`SKILL.md` bodies are content-addressed and deduplicated in `blobs/`.

Statuses are `cold`, `verified`, `published`, `rejected`, and `superseded`. Without an
available host `EvaluationService`, a valid proposal remains `cold`. Absence of a verifier is
never interpreted as success.

## Project Probes

A project-specific claim must name one or more probe files. Probes are read-only and accept
only UTF-8 or binary files addressed by relative path under the trusted cwd. Absolute paths,
`..`, missing files, oversized files, symlinks, and Windows junctions are rejected. Each probe
is captured by SHA-256 and re-read immediately before publication.

The model has no evolution-specific shell, Python, network, or arbitrary-path probe. Ordinary
coding tools remain governed by their own host and permission policies; their output cannot
stand in for a recorded project probe.

## Evaluation And Publication

Experience depends only on the host `EvaluationService` protocol in `run_agent_coding`; it
does not import `run_agent_evals`. The host owns suites, graders, thresholds, and budgets.
The candidate can freeze what is measured but cannot choose its grader or pass threshold.

Publication requires a passed `EvaluationReport` whose report ID, frozen request, candidate
ID, baseline digest, requested candidate digest, and measured digest all match the candidate.
Immediately before writing, publication rechecks:

- the content-addressed candidate blob and structural/threat validation;
- every project probe digest;
- current base digest;
- `created_by: evolution` ownership for an existing Skill;
- the legacy pinned flag;
- absence of a same-name Skill in the other formal scope for a new Skill.

The write holds the formal Skill root lock and atomically replaces exactly one `SKILL.md`.
Support files are never changed by a candidate. A successful publication appends a formal
ledger entry containing candidate, report, probe, source-session, and source-run provenance.
If ledger recording fails, the Skill body is restored. Startup reconciliation marks a
candidate published only when both the installed digest and formal ledger entry agree.

## Commands

| Command | Purpose |
| --- | --- |
| `/evolve status` | Show evaluation availability and candidate counts. |
| `/evolve candidates [status]` | List candidates, optionally by status. |
| `/evolve show <candidate-id>` | Show immutable metadata, operations, claims, and body. |
| `/evolve adopt <name> [--scope ...]` | Explicitly transfer an unpinned existing Skill to `created_by: evolution`. |
| `/evolve publish <candidate-id>` | Revalidate a passed report and atomically publish. |
| `/evolve reject <candidate-id> [reason]` | Append a terminal rejection event. |
| `/evolve ledger [name] [--scope ...]` | Inspect formal Skill ledger entries. |
| `/evolve rollback <ledger-id> [--scope ...]` | Restore the before-state after a safety capture. |

`/memory` is not part of this extension any more; it is registered by the `memory` built-in.
There is no review or curator command surface and no automatic stale/archive/consolidate pass.
Old `.usage.json`, `.archive/`, `.ledger.jsonl`, and ledger blobs are not deleted. The old
usage sidecar is read only for pinned compatibility; consultation counters are no longer
updated.

## Configuration

Supported environment variables are:

- `EXPERIENCE_SKILLS_WRITE_APPROVAL`
- `EXPERIENCE_SKILL_GUARD`, `EXPERIENCE_SKILL_LEDGER`
- `EXPERIENCE_EVOLUTION_SUITE`, `EXPERIENCE_EVOLUTION_SUITE_VERSION`
- `EXPERIENCE_EVOLUTION_BUDGET_SECONDS`

The old `EXPERIENCE_MEMORY_*` / `EXPERIENCE_USER_*` variables were replaced by
`HERMES_MEMORY_*` and are no longer read here. Review cadence, review inference, curator
interval, archive, consolidation, and backup environment variables are removed.

## Boundaries

- Skill candidates and published Skills are not memory: durable facts and preferences go to
  the `memory` extension, and `skill_manage` never edits those files.
- Candidate storage is durable isolation, not a claim that a candidate improves behavior.
- `verified` means the injected host report passed; it does not generalize beyond that suite.
- Publication is atomic for one `SKILL.md`, not a multi-file Skill transaction.
- Evaluation runs with learning writeback disabled cannot mutate candidate state or
  formal Skills, and the memory extension refuses its own writes under the same switch.
