# Run Agent Evaluation

`run_agent_evals` 使用真实 `CodingSession` 执行任务，并将可复核证据与归约结果分离。

## Coding campaign

```powershell
.\.venv\Scripts\run-agent-bench.exe run evals/coding/smoke/tasks.jsonl `
  --output-root .run/evals/smoke `
  --extension extensions/observability `
  --candidate-id baseline `
  --seed 0 `
  --concurrency 1

.\.venv\Scripts\run-agent-bench.exe rebuild .run/evals/smoke
```

输出包含：

- `manifest.json`：revision、平台、fixture digest、prompt hash、seed、candidate、并发与模型元数据。
- `trials/*.json`：工作区前后 digest、执行输出、verifier 退出码、调用与 token 元数据。
- `inventory.json`：每个 trial 的字节数和 SHA-256。
- `report.json`：pass rate、P50/P95、调用数和可用时的成本归约。
- `runtime/calls/*.jsonl` 与 `runtime/traces/*.jsonl`：物理调用账本和执行 spans。

`rebuild` 会校验 manifest、trial matrix、artifact path 和全部内容凭证；证据被修改后拒绝重建。

## Runtime benchmark

```powershell
.\.venv\Scripts\run-agent-bench.exe runtime
.\.venv\Scripts\run-agent-bench.exe runtime-rebuild .run/benchmarks/runtime/<run-id>
```

该命令覆盖 10,000 请求的生产 `TurnScheduler`、生产 Agent loop 上的合成异步 read tool，
以及开启 `fsync` 的生产 `TraceRecorder`。时延是机器相关数据；工具项是用于验证有界并发与
结果顺序的合成微基准，不能当作真实文件系统或线上接口性能。

## 指标规则

- 只有 status=`passed` 且 verifier 退出码为 0 才计为通过。
- logical calls 与 physical HTTP attempts 分开统计，重试按物理尝试归一化。
- 成本优先采用 provider 实报；否则在模型目录费率完整时标记为 `catalog_estimate`；仍不可估价时
  汇总为 `null`，不能写成零成本。
- 小规模 smoke 只证明链路可运行，不代表公开 benchmark 的泛化解决率。
