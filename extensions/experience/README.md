# Experience

Optional Session extension that keeps project and user experience as immutable, versioned
resources: USER preferences, MEMORY facts and Skills. It is loaded like any other extension and
adds nothing to Core or Gateway.

```powershell
run -e extensions/experience
run -e extensions/experience --session <id> --refresh-resources
```

## Assets and scopes

| Scope | Meaning |
| --- | --- |
| `project` | Facts and Skills that belong to the current project. |
| `user` | Preferences that apply to every project of this user. |

Asset keys are `user/<name>`, `memory/<name>` and `skill/<name>`. A project asset with the same key
as a user asset wins inside that project; the user asset is not modified.

Every write creates a new immutable resource version with a parent pointer, a source
(`manual` command receipt or fixed model input snapshot), an observation time, optional
`applies_to`, `invalidation_conditions` and `expires_at`. The published head is what new
snapshots select; existing sessions keep the versions captured at activation or `/reload`.

## Commands

```text
/experience list|search <project|user> [query]
/experience remember|propose <scope> <user|memory|skill> <name> <content>
/experience forget <scope> <kind> <name>
/experience candidates <scope>
/experience diff|publish <scope> <candidate-id>
/experience rollback <scope> <asset-key> <version>
/experience checkout <scope> <kind> <name>
/experience import <scope> <kind> <name> <base-version>
```

`remember` writes and publishes in one step for user/MEMORY content. Skills stay candidates and
need an explicit `publish`, because a Skill changes how later tasks are executed.

`checkout` writes a Markdown working copy under `<state>/experience/<scope-digest>/USER.md`,
`MEMORY.md` or `SKILL.md`. A second checkout never overwrites edits. `import` reads that file back
and fails when the published version moved, so concurrent edits become a reviewable candidate
instead of a silent overwrite. `rollback` points the head at an older version and keeps the
history of both versions.

## Model-facing tools

| Tool | Purpose |
| --- | --- |
| `memory` | Search/list published experience, or propose a candidate from the current fixed model input. |
| `experience_skill` | Load one Skill body from the Session's fixed experience index. |

The model can only propose. A proposal without a fixed input snapshot is rejected, proposals are
stored as `needs_evidence`, and publication needs an explicit command receipt. Skills are exposed
to the prompt as a bounded index; the body is loaded on demand and stays on the captured version
for the life of the session.

## Boundaries

- The extension is optional: without it coding, compaction, resume and Skills behave normally.
- No Mem0 integration, no legacy memory export/import and no migration path exists; state lives in
  the SQLite host services.
- Experience context is data, not permission: it cannot grant tool approvals.
- A durable completion can queue a review request. The trigger decides what is worth reviewing,
  keeps one review per run and policy version, honours a cooldown, and refuses auxiliary origins so
  a review cannot trigger another review. The registered `experience-review` task consumes one
  request under a claim, within a declared request and token budget, and may only read evidence,
  propose a candidate or inspect - it cannot publish or edit permissions.
- Promotion is bound to evidence: a candidate advances only on a report that measured that
  candidate's content hash. With no evaluation service available the candidate stays a candidate.
- Learning writeback is disabled for the duration of an evaluation run, so a measured trial cannot
  change the assets it is measuring.
- Stale marking and automatic archiving of low-use assets are not implemented yet.
