# Checkpoint 14: native experience management

Date: 2026-09-10. The full redesign remains active. This checkpoint records the P4 stage:
`extensions/experience` replaces the removed Mem0 integration with versioned, scoped assets.

## Implemented

- `extensions/experience` is an optional Session extension. It registers a read-only resource
  provider (`experience`, version 1), a `session_start` handler, an `/experience` command and two
  model-facing tools (`memory`, `experience_skill`). It adds nothing to Core or Gateway, and
  `run` uses it without starting either.
- Assets are `user/<name>`, `memory/<name>` and `skill/<name>` in `project` and `user` scopes,
  stored as immutable resource versions in the SQLite host services. Each version carries a parent
  pointer, a source (`manual` command receipt or a fixed model input snapshot), observation time,
  optional `applies_to`, `invalidation_conditions` and `expires_at`.
- `remember` publishes user/MEMORY content in one step. Skills stay candidates and need an explicit
  `publish`, because a Skill changes how later tasks run.
- The model can only propose. A proposal without a fixed input snapshot is rejected and stored as
  `needs_evidence`; publication requires an explicit command receipt. `experience_skill` loads a
  Skill body from the Session's captured index, so index and body stay on the same version.
- `checkout` writes a Markdown working copy (`USER.md`, `MEMORY.md`, `SKILL.md`) under the state
  directory. A second checkout never overwrites edits, and `import` fails when the published
  version moved, turning concurrent edits into a reviewable candidate instead of a silent
  overwrite. `rollback` points the head at an older version and keeps both versions' history.
- Expired or invalidated resources are omitted from new snapshots; existing sessions keep the
  versions captured at activation or `/reload`.

## Evidence

- `experience-tests.xml`: full Windows suite, 196 tests, 0 failures, 2 platform skips.
- `tests/redesign/test_experience.py`: 7 integration tests:
  `test_manual_preference_persists_scopes_refresh_forget_and_rollback`,
  `test_candidates_do_not_publish_and_concurrent_heads_do_not_lose_updates`,
  `test_model_tool_can_only_propose_with_actual_snapshot_evidence`,
  `test_source_proof_required_and_tool_rejects_publication_action`,
  `test_expired_resources_are_omitted_from_new_snapshots_and_extension_is_optional`,
  `test_markdown_checkout_preserves_edits_and_import_checks_base_version`,
  `test_skill_index_and_lazy_body_stay_on_the_same_version`.
- `extensions/experience/README.md` and `extensions/README.md` describe the commands and scopes.

## Limits and Remaining Work

`extensions/experience/extension.py` is 262 lines and `repository.py` is 232 lines, both above the
200-line limit the plan's structural rules set. The P5 work adds review, promotion and rollback
policy; that logic belongs in the new small modules the plan already names (`policy.py`,
`review.py`, `promotion.py`, `schema.py`) rather than in these two files, and the existing bulk
should be split at the same time.

There is no automatic review, no evaluation-gated promotion and no stale marking for automated
candidates. Every publication today is an explicit user action with a command receipt. There is no
`EvaluationService` on the host, so a candidate cannot be validated automatically, and
`Candidate.report_id` is never written. Those are the P5 and P6 stages.

No real model or real channel credentials were used. These results validate the storage, scope and
publication contracts, not learning effectiveness.
