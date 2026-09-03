# Run Agent 评测体系

更新日期：2026-09-03

Run Agent 的评测遵循一条原则：**模型声称完成不算完成，必须由环境事实、确定性验证器和可追溯证据共同判定。**

工程入口与命令细节另见：

- [`docs/swebench.md`](./docs/swebench.md)
- [`docs/sandbox.md`](./docs/sandbox.md)
- [`../evals/coding/README.md`](../evals/coding/README.md)

## 1. 当前可验证状态

| 能力 | 状态 | 证据 |
|---|---|---|
| Runtime / Harness 单元与架构回归 | 已接入 | `pytest`，覆盖权限、Trace、Harness Extension、Sandbox、SWE-bench adapter |
| 无 API 离线合约评测 | 已接入 | `evals/smoke/`，3 个 fixture case，CI 强制通过 |
| Verification / Correction Extension | 已接入 | Patch / syntax / diff / focused test；失败证据回写；默认最多 2 次 Repair |
| Coding Task 隔离 runner | 已接入 | `run-agent-coding-benchmark`，`evals/coding/smoke/`，支持 `--harness-mode` |
| Docker Sandbox 生命周期 | 已接入 | 短生命周期容器、hardening、adapter-only 与真实 lifecycle 测试 |
| 运行轨迹 | 已接入 | `.run/traces/*.jsonl` 与任务 artifact 目录；敏感字段脱敏 |
| GAIA/HLE 真实运行 driver | 已接入 | `python -m agents.evaluation.benchmarks ...`，通用 Agent 补充评测 |
| SWE-bench Verified adapter | 已接入 | `agents/evaluation/swebench/`，`run-agent-swebench download/inspect/campaign` |
| SWE-bench 官方 grading | 可选接入 | `campaign --grade`，需要 Docker 与 `.[swebench]`（钉死 `swebench==5.0.2`） |
| Harness 消融臂 | 已接入 | `baseline` / `verifier` / `full`；`pi-rewrite-ablation` 五题机制跑 |
| 数据/轨迹哈希与 manifest | 已接入 | Git、依赖、Tool/Policy hash、运行配置和逐题 Trace / Patch hash |
| GAIA/HLE / SWE-bench 正式成绩 | **待重新运行并验收** | 必须保留模型、数据哈希、逐题 prediction、Trace 与官方 report 后才能写入简历 |

旧文档曾写有 GAIA 53.3%、HLE 20.2%、fold 提升 8.6 个百分点等数值，但仓库中没有与这些数字一一对应的运行 manifest、逐题 prediction、原始 Trace 和独立复核脚本。因此这些数字现在只视为**历史口径，不作为已验证项目结论，也不建议直接写入简历**。

## 2. 评测闭环

```text
固定数据与配置
  -> reset / 隔离 Memory 与 Skills
  -> Harness 执行（可选 baseline/verifier/full）
  -> 写 Trace / Patch / Verification / Session DB
  -> 提取最终答案与工具轨迹
  -> trace integrity verifier
  -> correctness verifier
  -> process verifier
  -> safety verifier
  -> 保存 manifest + predictions + hashes
  -> （SWE-bench）可选官方 Docker grading
  -> 分析失败样本并加入回归集
```

对应代码：

- `agents/runtime/tracing.py`：Append-only JSONL Trace、敏感字段脱敏、事件序号
- `agents/runtime/contracts.py`：事件、工具和评测数据契约
- `agents/harness/`：TaskSpec / TaskResult、Extension 消融栈
- `agents/evaluation/verifiers.py`：确定性 correctness/process/safety verifier
- `agents/evaluation/runner.py`：离线 Trace replay 与哈希报告
- `agents/evaluation/benchmarks.py`：GAIA/HLE adapter 与 live runner
- `agents/evaluation/coding.py`：隔离仓库 Coding Task runner
- `agents/evaluation/swebench/`：钉死数据、非泄漏 prompt、patch campaign、官方 grader
- `evals/smoke/`：无需模型的评测合约测试
- `evals/coding/smoke/`：无需/有限 API 的 Coding fixture

## 3. 指标分层

### 3.1 Correctness

- `exact_answer`：严格答案匹配
- `contains`：必须包含的事实
- `regex`：结构化输出约束
- Coding / SWE-bench：`resolved` / 官方 `correct`（Docker harness 权威）
- GAIA/HLE live runner：规范化后的 `Pass@1`

### 3.2 Process

- 是否调用必要工具
- event sequence / run id / call id 是否一致
- 每个 ToolCall 是否存在最终 permission decision 和唯一 ToolResult
- 工具调用次数是否达到最低要求
- 工具错误是否超过上限
- 是否产生正常的完成事件
- 是否重复执行同一 tool call

### 3.3 Safety

安全指标是 veto，而不是可以被答案质量抵消的软分：

- 禁止工具是否被调用
- Plan Mode 中写入/Shell 是否被权限层拒绝
- 已拒绝的工具是否仍被实际执行
- 外部动作是否留下 permission decision

离线 verifier 的默认总分为：

```text
score = 0.60 * correctness + 0.25 * process + 0.15 * safety
```

但任何 safety check 失败都会令该 case 直接失败。
Trace 完整性失败同样是硬失败，不能被答案正确率抵消。

## 4. 快速运行

### 4.1 无 API smoke suite

```powershell
python -m agents.evaluation.cli evals/smoke/cases.jsonl `
  --traces evals/smoke/traces `
  --fail-on-regression
```

输出报告：`.run/evals/latest.json`。

注意：`evals/smoke/traces/` 是用于验证评测代码的 fixture，不是模型能力成绩。

### 4.2 GAIA 小样本真实运行

```powershell
python -m agents.evaluation.benchmarks gaia `
  --problem-type text `
  --limit 10 `
  --seed 42 `
  --model deepseek-chat
```

### 4.3 HLE 小样本真实运行

```powershell
python -m agents.evaluation.benchmarks hle `
  --problem-type text `
  --limit 10 `
  --seed 42 `
  --model deepseek-chat
```

当前 Runtime 没有图像输入 adapter，因此无筛选 campaign 默认排除 GAIA/HLE `mm` 样本；显式请求 `--problem-type mm` 会被拒绝。不能把图片路径拼成文本后宣称完成多模态评测。本地数据包含 GAIA 24 个、HLE 113 个 `mm` case；当前可直接运行 528 个 text/file case。纯文本实验应显式使用 `--problem-type text`；`--allow-unsupported-mm` 只用于把多模态 case 记录为 unsupported failure。

### 4.4 Coding Task 快速回归与消融

```powershell
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl `
  --model <model> `
  --sandbox docker `
  --harness-mode full `
  --max-cost 0.50
```

无模型生命周期检查：

```powershell
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl --adapter-only --sandbox local
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl --adapter-only --sandbox docker
```

每个任务从固定 fixture 复制出临时仓库，先确认 seeded task 的 acceptance command 失败，再运行 Agent，最后保存 Patch、Trace、验证报告和 resolved 状态。它用于快速回归以及 verifier/correction 的配对消融，不替代 SWE-bench Verified 的正式结果。

每次运行生成：

```text
.run/benchmark-runs/<benchmark>-<timestamp>-<model>/
├── manifest.json       # 数据哈希、模型、协议、seed、汇总
├── predictions.jsonl   # 逐题答案、reference、token、耗时、错误
└── traces/             # 每题完整事件轨迹
```

默认关闭长期 Memory、Skills 和 Skill 自进化，避免跨 case 污染。只有做消融实验时才显式加 `--with-memory` 或 `--with-skills`。

### 4.5 SWE-bench Verified 主评测

Coding Agent 的主 benchmark 使用官方 `SWE-bench/SWE-bench_Verified` 的 `test` split，共 500 个实例。数据固定为 `data/SWE-bench_Verified/test-00000-of-00001.parquet`，并在 `dataset-manifest.json` 中保存来源、字节数、行数和 SHA-256。

```powershell
run-agent-swebench download
run-agent-swebench inspect --limit 5
run-agent-swebench campaign --limit 2 --model <model> --max-cost 2.00 --sandbox docker --harness-mode full
run-agent-swebench campaign --limit 2 --model <model> --max-cost 2.00 --sandbox docker --harness-mode full --grade
```

Adapter 仅把 `problem_statement` 和公开 hints 注入 Agent；gold `patch`、`test_patch`、`eval_script` 和测试清单只用于证据保存或官方 grading，避免答案泄漏。`campaign` 先在临时 checkout 中生成 patch，再保存 prediction、patch、Trace、sandbox、verification 与 Session DB；`--grade` 调用官方 `swebench.harness.run_evaluation`，由 Docker 中的 SWE-bench instance image 执行真实测试。

完整命令约定、`--adapter-only`、`--sandbox-image` 与五题机制跑见 [`docs/swebench.md`](./docs/swebench.md)。

## 5. 正式实验设计

### 5.1 Harness / Verifier / Correction 消融

至少比较三臂（同一模型、Provider、实例集合、seed、预算、镜像）：

| Arm | Verification | Correction | CLI |
|---|---:|---:|---|
| baseline | off | off | `--harness-mode baseline` |
| verifier | on | off | `--harness-mode verifier` |
| full | on | on | `--harness-mode full` |

主指标包括 resolved rate、first-pass rate、repair success rate、regression rate、unsafe-action rate、平均修复轮数、token 与成本。首轮实验使用本地 Coding Task 快速回归集；确认 Harness 行为后，再把同样的配置轴用于 SWE-bench Verified campaign。

机制复现入口：

```powershell
run-agent-swebench pi-rewrite-ablation --grade --model <model> `
  --max-cost 0.75 --output .run/swebench-pi-rewrite
```

### 5.2 Fold 消融

目标：验证结构化 fold 是否提升长程任务完成率，而不只是降低 token。

需要保持同一模型/provider、任务顺序、seed、turn/cost cap、工具、权限和 Memory/Skills 开关。对 `fold=off` 与 `fold=on` 两臂至少报告：

- 配对任务 Pass@1
- token 与成本
- 工具错误率
- compact 次数
- 失败类型分布
- 配对 bootstrap 置信区间或 McNemar 检验

当前 GAIA/HLE runner 已保存这些指标需要的逐题数据和 Trace，并支持 `--disable-fold` 生成关闭 fold 的对照臂。两次运行必须使用同一 case id 集合，然后执行：

```powershell
python -m agents.evaluation.compare `
  .run/benchmark-runs/<baseline>/predictions.jsonl `
  .run/benchmark-runs/<candidate>/predictions.jsonl `
  --output .run/benchmark-runs/fold-comparison.json `
  --allow-difference fold_enabled
```

比较报告包含 Pass@1 绝对差、配对 bootstrap 95% CI、McNemar 精确检验和平均 token 变化。

比较器会读取 predictions 邻接的 `manifest.json`。除显式声明的消融轴外，只要模型、数据、provider、temperature、权限、turn/cost cap 或其他关键配置不同，就拒绝把两次运行称为配对消融。

### 5.3 Memory / Skills 消融

推荐四臂：

| Arm | Memory | Skills |
|---|---:|---:|
| baseline | off | off |
| memory | on | off |
| skills | off | on |
| combined | on | on |

评测必须使用独立 holdout case；不得在同一批测试题上自进化后再宣称提升。

## 6. CI 门禁

GitHub Actions 当前在 Python 3.11、3.12、3.13 上执行：

1. `compileall`
2. `pytest`
3. 离线 smoke replay

真实模型 benchmark 不进入普通 PR CI，避免不稳定网络和不可控费用；建议通过手动 workflow 或本地 campaign 运行，并提交脱敏后的 manifest/summary。

## 7. 简历数字的发布标准

只有同时满足以下条件的指标才写入简历：

- 固定数据版本并保存 SHA-256
- 明确模型、provider、seed、任务筛选条件、harness-mode / sandbox
- 每个 case 保留 prediction 与 Trace（SWE-bench 另需官方 report）
- 失败 case 不删除、不事后换题
- 统计脚本可重复运行
- 数字与 committed manifest 一致
- A/B 对同一批任务配对比较并报告统计不确定性

没有证据包的数字，宁可不写。当前仓库**不声称**已完成 50 题或 500 题 SWE-bench 正式成绩。
