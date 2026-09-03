# Coding Agent task format

`tasks.jsonl` contains one isolated fixture repository per line:

```json
{"id":"python-off-by-one","fixture":"fixtures/python-off-by-one","prompt":"修复边界条件并确保测试通过。","verify":[["python","-m","pytest","-q"]],"tags":["bug-fix","python"]}
```

The campaign copies each fixture into a temporary workspace, verifies that the
seeded task initially fails, runs the Agent, executes the declared acceptance
commands, and stores the patch, Trace and result.  Real resume metrics should
come from a fixed 30–50 task manifest committed with its fixtures or from a
public coding benchmark adapter.

Two deliberately failing no-API fixtures are checked in under `smoke/` to
validate task packaging.  A live smoke campaign can be launched with:

```powershell
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl --limit 2 --model <model> --max-cost 0.50
```

To validate the task lifecycle without an API key or model call, use
`--adapter-only`. Docker mode exercises the real container when the daemon and
image are available:

```powershell
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl --adapter-only --sandbox local
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl --adapter-only --sandbox docker
```
