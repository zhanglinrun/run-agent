# SWE-bench Verified

`agents/evaluation/swebench/` pins the public `SWE-bench/SWE-bench_Verified`
test split and verifies its SHA-256 before use. Prompts include only the issue
statement, repository and optional public hints. Gold patches, test patches,
and hidden evaluator details are never sent to the model.

```text
swebench/
├── models.py       # task and prediction contracts
├── adapter.py      # pinned dataset, clean checkout and patch export
├── manifest.py     # Git / Tool / Policy / image evidence hashes
├── evaluator.py    # official SWE-bench harness invocation
└── runner.py       # campaign lifecycle and artifacts
```

Generate predictions with an explicit cost limit:

```powershell
python -m agents.evaluation.swebench campaign --limit 2 --max-cost 2.00 --sandbox docker --harness-mode full
```

开发调试可使用低成本模型；当前固定机制实验使用 `.env` 中的
`MODEL=gpt-5.6-luna`。运行时仍建议通过 `--model gpt-5.6-luna`
显式传值，确保 manifest 中记录的模型与实际请求一致。三组消融必须使用同一
模型、Provider、任务、顺序、预算和镜像。

需要只验证 checkout、镜像启动、Patch 导出和 artifact，而不调用模型时，使用
`--adapter-only`；该模式不要求 API Key：

```powershell
python -m agents.evaluation.swebench campaign --instance-id pallets__flask-5014 `
  --adapter-only --sandbox docker --sandbox-image run-agent-python-sandbox:latest
```

`--sandbox-image` is an explicit smoke/debug override. Formal runs omit it and
use each official task image with the checkout mounted at `/testbed`.

## Harness ablation

Use the same model, provider, selected instance IDs, seed, image, budget and
resource limits for all arms:

```powershell
run-agent-swebench campaign --limit 50 --seed 42 --model <model> --max-cost <per-task-usd> --harness-mode baseline --grade
run-agent-swebench campaign --limit 50 --seed 42 --model <model> --max-cost <per-task-usd> --harness-mode verifier --grade
run-agent-swebench campaign --limit 50 --seed 42 --model <model> --max-cost <per-task-usd> --harness-mode full --grade
```

- `baseline`: Runtime Verification off, Correction off.
- `verifier`: Runtime Verification on, Correction off; failed patches are retained for official grading.
- `full`: Runtime Verification and bounded Correction both on.

After official grading, `results.jsonl` receives per-instance `correct` and
`official_status` fields. Use `run-agent-compare` on two scored result files;
declare only the intended ablation fields as allowed manifest differences.

The adapter stores per-instance prompt, patch, trace, sandbox and verification
artifacts, plus SQLite Session DB, Patch Candidate hashes and
`mechanism_report.md`. Pass `--grade` only after installing the pinned official
`swebench==5.0.2` package; the official Docker harness remains the source of
truth for pass/fail.

The complete public campaign is intentionally not automatic. First run a
no-model adapter smoke, then a fixed pilot with one model and identical
budget/configuration across Harness ablations.

The repository also exposes the exact five-case mechanism run used during the
pi-style rewrite:

```powershell
run-agent-swebench pi-rewrite-ablation --grade --model gpt-5.6-luna `
  --max-cost 0.75 --output .run/swebench-pi-rewrite
```

This command locks the five instance IDs, seed, temperature, turn budget,
repair budget, network policy and worker count, runs baseline/verifier/full in
sequence, and writes `paired_comparison.json` after official grading.

On September 2, 2026, the pinned dataset check, real Docker lifecycle test,
two-fixture Docker adapter smoke and one live Coding Harness smoke passed on
this machine. No 50-case or 500-case SWE-bench score is claimed yet.
