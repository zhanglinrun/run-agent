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

该执行器继承应用的内置扩展默认值，`--extension` 可追加扩展，`--trust-project` 允许加载项目资源。独立任务工作区不等于独立用户记忆或 OS 沙箱；需要控制经验状态的实验应明确设置状态目录、初始资产及是否共享。当前执行器在主任务结束后关闭应用，没有单独等待自动复盘完成的阶段。

`--seed` 可重复指定以建立采样矩阵；它是 trial 标识，当前不会透传为模型 API 的随机种子。`--keep-workspaces` 保留工作区供诊断，否则结束后清理。

输出包含：

- `manifest.json`：revision、平台、fixture digest、prompt hash、seed、candidate、并发与模型元数据。
- `trials/*.json`：工作区前后 digest、执行输出、verifier 退出码、调用与 token 元数据。
- `inventory.json`：每个 trial 的字节数和 SHA-256。
- `report.json`：pass rate、P50/P95、调用数和可用时的成本归约。

调用账本与执行 spans 不再写成逐行文件，而是以 stream 形式落在 SQLite 的 `observations`
表（`<state-dir>/state.sqlite3`），丢弃与失败计数在 `observation_health` 表。这保证证据可以
被事务化查询和一致性备份，而不是扫描散落的文件。

`rebuild` 会校验 manifest、trial matrix、artifact path 和全部内容凭证；证据被修改后拒绝重建。

## Runtime benchmark

```powershell
.\.venv\Scripts\run.exe bench runtime
.\.venv\Scripts\run.exe bench runtime-rebuild .run/benchmarks/runtime/<run-id>
```

该命令测量两项：生产 Agent 循环中合成异步只读工具的串行/并发耗时，以及启用 SQLite `TraceRecorder` 相对于无追踪循环的开销，后者包含持久化刷盘。

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
