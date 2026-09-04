# Official optional extensions

These capabilities use the same filesystem extension contract as user extensions. None is loaded by
the core session automatically.

| Directory | Registration |
| --- | --- |
| `mem0` | Mem0-backed `memory` tool |
| `mcp` | MCP Streamable HTTP bridge tool |
| `observability` | Per-session JSONL span recorder and `/trace` |
| `permission_policy` | Mutating-tool policy hook |
| `plan_mode` | Read-only `/plan` policy |
| `verification` | Structured `verify` tool |

Load one directly while developing:

```powershell
uv run run-agent -e extensions/mem0 --print "Remember the package manager"
```

Install a trusted extension for normal user-level discovery:

```powershell
uv run run-agent install extensions/mem0
uv run run-agent install extensions/permission_policy
```

Load the directory explicitly to exercise the complete official set:

```powershell
uv run run-agent -e extensions
```

Extensions execute with the current user's OS permissions. Installation is a trust decision, not a
sandbox boundary.
