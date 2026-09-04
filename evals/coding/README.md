# Coding task fixtures

`tasks.jsonl` 每行定义一个隔离任务：

```json
{"id":"python-off-by-one","fixture":"fixtures/python-off-by-one","prompt":"修复边界条件并确保测试通过。","verify":[["python","-m","pytest","-q"]],"tags":["bug-fix","python"]}
```

字段：

- `id`：campaign 内唯一任务标识。
- `fixture`：相对 manifest 的只读种子目录；每个 trial 会复制到独立临时工作区。
- `prompt`：交给标准 `CodingSession` 的任务。
- `verify`：模型执行结束后运行的确定性验收命令列表。
- `timeout_seconds`：单条 verifier 超时，默认 120 秒。
- `tags`：仅用于任务分类。

运行两个故意以失败状态开局的 smoke fixture：

```powershell
uv run run-agent-bench run evals/coding/smoke/tasks.jsonl `
  --output-root .run/evals/coding-smoke `
  --extension extensions/observability `
  --candidate-id smoke
uv run run-agent-bench rebuild .run/evals/coding-smoke
```

正式简历指标应扩大并冻结任务集，保留相同 task id 的 baseline/candidate 配对；当前两题仅用于
验证复制、Agent 修改、外部 verifier、调用账本、trace 与离线重建的端到端链路。
