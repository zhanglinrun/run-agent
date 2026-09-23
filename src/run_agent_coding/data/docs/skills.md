# Run Agent skills and prompt templates

Skills provide reusable task knowledge. Prompt templates save prompts that users invoke by name.

## Skills

A skill follows the Agent Skills structure:

```text
<skills-dir>/<skill-name>/SKILL.md
```

Run Agent loads user and project skills in increasing precedence:

1. `~/.run/skills/`
2. `~/.agents/skills/`
3. `<cwd>/.run/skills/`
4. `<cwd>/.agents/skills/`

Run Agent's own product knowledge is regular packaged documentation, not a built-in skill, so it does not appear in the user's skill list or compete with user skill names.

A higher-precedence skill with the same name overrides the lower one. Run Agent places only each skill's name, description, and path in the system prompt; the model reads the full file when its description matches the task. Use `/skill:<name>` for explicit invocation.

Project Skills require project trust. Loaded Skill packages are frozen in the session's
resource snapshot; edits become available after `/reload` or a new session. The default
experience extension exposes `skill_manage` only for `list`, `view`, and `propose`.

`propose` writes an immutable candidate outside every Skill loader root. A candidate remains
`cold` when the host has no `EvaluationService`; it becomes publishable only after a passed
report binds the exact candidate digest. Existing Skills are user-owned unless their
frontmatter says `created_by: evolution` or the user explicitly runs `/evolve adopt <name>`.
Pinned Skills, changed base digests, changed project probes, and mismatched evaluation reports
are refused.

Use `/evolve status|candidates|show|adopt|publish|reject|ledger|rollback` to inspect and control
the formal lifecycle. Publication revalidates the candidate, report, base, ownership and
trusted-project probe digests under the Skill root lock, then atomically replaces one
`SKILL.md` and appends report/run/probe provenance to the ledger. Candidate evolution changes
exactly one formal `SKILL.md`; support files remain unchanged. Old usage, archive and ledger
data remain readable for migration and rollback.

The default `curator` extension adds maintenance on top of this lifecycle and never edits a Skill body. It anchors inactivity on the newest of `last_consulted_at` (its own `<paths.extension_state_dir>/curator/usage.jsonl`, bounded by `CURATOR_USAGE_LOOKBACK_DAYS`), the newest Skill-ledger mutation that is not an `archive`, and the `SKILL.md` mtime, then applies the defaults: `stale` after `CURATOR_STALE_AFTER_DAYS` (30), archive after `CURATOR_ARCHIVE_AFTER_DAYS` (90), and reactivation when a stale Skill becomes active again. Archiving moves the whole `<skills-root>/<name>/` directory to `<skills-root>/.archive/<name>` (a second copy gets a `-<timestamp>` suffix) and is recorded in the Skill ledger; nothing is deleted, and `/curator restore <name>` moves it back. Protection is default-deny: pinned Skills (legacy `.usage.json`) and names in `CURATOR_PROTECTED_SKILL_NAMES` are never touched automatically; project Skills are only marked `stale` (`CURATOR_AUTO_ARCHIVE_PROJECT_SKILLS=false`), and user-written Skills — frontmatter without `created_by: evolution`, including the `created_by: user` default — are never archived (`CURATOR_AUTO_ARCHIVE_USER_SKILLS=false`). A curator consolidation is only a cold candidate and still has to pass the `/evolve publish` gate.

A skill with `disable-model-invocation: true` in its `SKILL.md` frontmatter is excluded from the system prompt entirely, so the model cannot invoke it on its own. The skill stays loaded and remains available through explicit `/skill:<name>` invocation and the `/skills` picker.

## Prompt templates

Templates load from user and project `.run/prompts/` and `.agents/prompts/` directories. They are prompt shortcuts, not background knowledge, and support Pi-compatible argument placeholders such as `$1`, `$@`, `$ARGUMENTS`, defaults, and slices. Legacy `{{ arguments }}` and `{{ args }}` placeholders remain supported.

Use a skill for reference know-how and a template for a frequently repeated prompt. Run `/reload` after changing resources in an active TUI session.

When modifying Run Agent's resource system, read `run_agent_coding/skills.py` and
`run_agent_coding/resources.py`, then test discovery, precedence, diagnostics,
prompt formatting, and reload behavior.
