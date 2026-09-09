# Official optional extensions

These capabilities use the same filesystem extension contract as user extensions. None is loaded by
the core session automatically.

| Directory | Registration |
| --- | --- |
| `mcp` | MCP Streamable HTTP bridge tool |
| `observability` | Session-scoped SQLite spans and `/trace` |
| `permission_policy` | Mutating-tool policy hook |
| `plan_mode` | Read-only `/plan` policy |
| `verification` | Structured `verify` tool |

Load one directly while developing:

```powershell
.\.venv\Scripts\run.exe -e extensions/plan_mode --print "Plan the repository changes"
```

Install a trusted extension for normal user-level discovery:

```powershell
.\.venv\Scripts\run.exe install extensions/permission_policy
```

Load the directory explicitly to exercise the complete official set:

```powershell
.\.venv\Scripts\run.exe -e extensions
```

Extensions execute with the current user's OS permissions. Installation is a trust decision, not a
sandbox boundary.
