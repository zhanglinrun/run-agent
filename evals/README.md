# Run Agent Evaluation

`run_agent_evals` 通过 `CodingApplication` / `CodingSession` 执行任务，并将可复核证据与归约结果分离。

应用的评测命令入口是 `run bench ...`；SWE-bench 的专用实验脚本见下文。

## Coding campaign

```powershell
.\.venv\Scripts\run.exe bench run <tasks.jsonl> `
  --output-root .run/evals/smoke `
  --candidate-id baseline `
  --seed 0 `
  --concurrency 1

.\.venv\Scripts\run.exe bench rebuild .run/evals/smoke
```

`bench run` 接受 JSONL 单文件任务清单；其中的 verifier 在 Agent 工作区运行，适合链路验证。目录任务及独立验收见 [coding/README.md](coding/README.md)。

该执行器继承应用的内置扩展默认值，`--extension` 可追加扩展，`--trust-project` 允许加载项目资源。独立任务工作区不等于独立用户记忆或 OS 沙箱；普通 coding trial 全程启用 `writeback_disabled()`，既不能修改正式经验，也不能推进候选状态。Skill 演进使用独立的 paired evaluator 和隔离 state roots。

`--seed` 可重复指定以建立采样矩阵；它是 trial 标识，当前不会透传为模型 API 的随机种子。`--keep-workspaces` 保留工作区供诊断，否则结束后清理。

输出包含：

- `manifest.json`：revision、平台、fixture digest、prompt hash、seed、candidate、并发与模型元数据。
- `trials/*.json`：工作区前后 digest、执行输出、verifier 退出码、调用与 token 元数据。
- `inventory.json`：每个 trial 的字节数和 SHA-256。
- `report.json`：pass rate、P50/P95、调用数和可用时的成本归约。

调用账本与执行 spans 以 stream 形式追加到 `<state-dir>/logs/observations.jsonl`。
评测当时的模型输入写到该 trial 工作区的 `eval-input.json`，不进会话库。

`rebuild` 会校验 manifest、trial matrix、artifact path 和全部内容凭证；证据被修改后拒绝重建。

## Context benchmark

```powershell
.\.venv\Scripts\run.exe bench context
.\.venv\Scripts\run.exe bench context .run/benchmarks/context/local
.\.venv\Scripts\run.exe bench context --output-root .run/benchmarks/context/local
.\.venv\Scripts\run.exe bench context-rebuild .run/benchmarks/context/local
```

`context` 完全离线运行，不调用模型。固定的 provider-neutral 样本分别包含超长工具结果、一组并行 tool calls 和多轮工具对话；命令用相同输入比较 `summary-only` 与 `cheap-first` 的 before/after token 估算、L1/L2/L3 使用次数、artifact 数、`prepare` P50/P95 时延、工具调用/结果配对有效性及仍需摘要的样本数。未指定目录时，证据写入 `.run/benchmarks/context/<run-id>`；也可使用可选位置参数或 `--output-root` 指定目录。

输出的 `evidence.json` 保存逐样本原始测量和 fixture digest，`inventory.json` 保存证据及内容寻址 blob 的大小与 SHA-256，`report.json` 保存两种策略的归约对比。`context-rebuild` 不重新执行 pipeline，只校验 evidence、inventory、artifact 和 report 的内容凭证并离线重建摘要；任何已冻结内容被修改都会拒绝重建。

## Skill evolution gate

```powershell
.\.venv\Scripts\run.exe bench evolve evals/evolution/config.toml `
  --skill python-config `
  --scope user `
  --output-root .run/evals/evolution-config

.\.venv\Scripts\run.exe bench evolve-rebuild .run/evals/evolution-config
```

`evolve` selects an explicit `--candidate-id` or the newest pending candidate for the Skill. Baseline and candidate are installed into separate state roots, explicitly invoked, and run three times per selection/test task with the same provider settings. A task passes an arm when at least two trials pass its hidden dual-proposition grader. Publication requires no infrastructure errors, no selection regression and at least one selection improvement. Test tasks are reported but never participate in the gate.

The command records frozen suite/task hashes, formal and candidate Skill digests, per-trial grader details, calls/tokens/cost metadata, source records, inventory and `run-agent.evolution-report.v1`. A passing report is not enough by itself: the Experience publisher rechecks report binding, project probes, ownership, pin and base digest under the Skill lock. `evolve-rebuild` verifies every evidence hash and recomputes the gate without model execution. Controlled results apply only to the declared task family.

## Runtime benchmark

```powershell
.\.venv\Scripts\run.exe bench runtime
.\.venv\Scripts\run.exe bench runtime-rebuild .run/benchmarks/runtime/<run-id>
```

该命令测量两项：生产 Agent 循环中合成异步只读工具的串行/并发耗时，以及启用 JSONL `TraceRecorder` 相对于无追踪循环的开销，后者包含持久化刷盘。

默认每批 8 个工具调用、每个调用模拟 20 ms 延迟，工具对比和追踪对比各重复 9 次。可用 `--tool-calls`、`--tool-delay-ms`、`--tool-repeats` 和 `--trace-repeats` 调整。时延依赖机器条件；合成工具数据不能当作真实文件系统或线上接口性能。

## SWE-bench 实验

[`scripts/run_swebench_all_extensions.py`](../scripts/run_swebench_all_extensions.py) 是 Windows 下的专用运行器，与上述自定义 fixture 评测分开。它需要本地 HAL Mini 50 题清单、Django/Sphinx Git 缓存、模型配置、官方评分器 Python 环境和 50 个已缓存的 Docker 评测镜像；这些本地数据不随仓库分发。

运行器提供 `prepare`、`preflight`、`solve`、`grade`、`status` 阶段。前两步冻结输入和检查环境，`solve` 才调用真实模型，`grade` 使用官方容器评分。每题 3 次独立采样，各有只包含基线提交的 Git 仓库、工具 Python 环境和经验目录；保存扩展激活、复盘状态、结果及补丁，异常和空补丁保留在分母。工作区隔离和权限检查不构成 OS 沙箱。

结果必须标明题目集合、采样次数、模型参数、代码快照、实际启用和使用的扩展、评分错误及成本来源。工作区或执行环境已清理的旧 campaign 应作为历史证据查阅，新实验使用新目录。

## 指标规则

- 只有 status=`passed` 且 verifier 退出码为 0 才计为通过。
- logical calls 与 physical HTTP attempts 分开统计，重试按物理尝试归一化。
- 成本只采用 provider 实报；provider 没有报价时汇总为 `null`，不能写成零成本。
- 小规模 smoke 只证明链路可运行，不代表公开 benchmark 的泛化解决率。
- 每题 3 次采样时，Pass@1 是全部尝试的平均通过率，Pass@3 是至少一次通过的题目比例，Pass^3 是三次均通过的题目比例。局部单次结果不能替代完整题集或三次采样指标。
- 扩展已加载、复盘已执行、经验写入成功和后续任务受益是不同证据。学习效果需要独立的后续任务或对照实验；Agent 的 `succeeded` 状态也不等于通过 grader。
