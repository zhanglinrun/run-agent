# Examples

## Python extension

[`extensions/audit.py`](./extensions/audit.py) implements the public versioned
`setup(api)` contract. Load it explicitly:

```powershell
python -m agents -e examples/extensions/audit.py "/audit demo"
```

The module registers a command and a per-turn prompt contribution. It declares a
dependency on `workspace-tools` and checks `api.api_version`.

## Structured session memory

`sessions/` contains static examples of the episode/working/tool fold shape:

| File | Description |
| --- | --- |
| `demo.folded-memory.latest.json` | One structured fold snapshot |
| `demo.folded-memory.jsonl` | Append-only fold history example |

The current runtime stores authoritative session entries in SQLite through
`agents/session/reducer.py`. `agents/extensions/context.py` writes a structured
compaction entry and canonical copies of the retained recent-message window, so
resume projects the fold plus recent messages without replaying the raw prefix.

Skill candidate/evaluation artifacts remain under the ignored
`.run/skill-evolution/` runtime directory because they can grow quickly.
