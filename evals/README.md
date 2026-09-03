# Evaluation suites

`evals/smoke/` is a deterministic, no-API-key contract suite. It verifies the
trace schema, tool-process checks, permission-denial evidence and multi-tool
regression logic. The checked-in traces are fixtures, not claimed model scores.

Run it with:

```powershell
python -m agents.evaluation.cli evals/smoke/cases.jsonl --traces evals/smoke/traces --fail-on-regression
```

Real model runs write append-only traces to `.run/traces/`. A benchmark driver
can name or copy each trace as `<case-id>.jsonl`, then replay the same verifier
against it. Reports are stored under `.run/evals/` and include hashes for the
dataset and every trace.
