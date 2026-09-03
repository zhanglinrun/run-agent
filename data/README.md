# Benchmark 数据集

本目录用于本地放置 Run Agent 的 benchmark 数据。大体积数据文件默认不提交 Git，仅提交本说明；使用者需要按数据集许可自行准备数据。

## 目录说明

| 目录 | 规模（约） | 用途 |
|------|-----------|------|
| `GAIA/` | 165 题 + 附件 | 通用 Agent、多步检索与文件处理补充评测 |
| `HLE/` | 500 题 + 图片 | 高难度推理补充评测；当前仅支持文本子集 |
| `ToolBench/` | 6 个 instruction 文件 | 工具调用与 API 编排 |
| `API-Bank/` | 484 文件 | 多 API 场景与 lv1–lv3 样本 |
| `WebShop/` | 1 文件 | 电商导航类任务 |
| `RestBench/` | 4 文件 | REST API（Spotify / TMDB） |
| `ToolHop/` | 1 文件 | 多跳工具推理 |
| `ALFWorld/` | 1 文件 | 文本环境交互 |
| `SWE-bench_Verified/` | 500 题 parquet | Coding Agent 主评测；真实 GitHub issue + 官方 Docker grader |

## SWE-bench Verified（主评测）

官方数据源：`SWE-bench/SWE-bench_Verified`，`test` split，共 500 个 instance。当前文件为：

- `SWE-bench_Verified/test-00000-of-00001.parquet`
- SHA-256：`030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25`
- `SWE-bench_Verified/dataset-manifest.json`：下载时间、行数、来源和哈希

下载或校验：

```powershell
run-agent-swebench download
```

查看元数据并生成 patch：

```powershell
run-agent-swebench inspect --limit 5
run-agent-swebench campaign --limit 2 --model <model>
```

Adapter 只把 `problem_statement` 和 issue hints 放入模型上下文；`patch`、`test_patch`、`eval_script` 和 `FAIL_TO_PASS` 仅用于保存证据或交给官方 grader，避免答案泄漏。官方 grading：

```powershell
pip install -e ".[swebench]"
run-agent-swebench campaign --limit 2 --model <model> --grade
```

`--grade` 会调用 `swebench.harness.run_evaluation`，因此需要 Docker 能拉取 SWE-bench 官方 instance images。500 题正式结果必须提交逐题 predictions、官方 report、manifest 和数据哈希，不能只报告一个未经复核的 pass rate。

## GAIA / HLE 补充评测（元数据共 665 题）

Coding Agent 的主评测是隔离仓库中的 bug-fix / feature tasks。GAIA + HLE 用于补充验证通用 Agent、工具使用和上下文能力，详见 [study/eval-benchmarks.md](../study/eval-benchmarks.md)。

- `GAIA/all.json` — 165 题元数据
- `GAIA/files/` — 题目附件（PDF、CSV、图片等）
- `HLE/all_500.json` — 500 题元数据
- `HLE/images/` — 多模态图片

当前 Runtime 没有图像输入 adapter。本地 GAIA 有 24 个、HLE 有 113 个 `mm` case，共 137 个；无筛选 campaign 默认排除它们，显式请求 `mm` 会被拒绝。不能把图片路径拼入文本后宣称完成多模态评测。当前可直接运行 528 个 text/file case。“665 题正式结果”必须等视觉输入支持，或明确把 137 个 unsupported case 计入失败后才能发布。

## 使用方式

仓库已提供 GAIA/HLE driver：

```powershell
python -m agents.evaluation.benchmarks gaia --problem-type text --limit 10 --seed 42
python -m agents.evaluation.benchmarks hle --problem-type text --limit 10 --seed 42
```

默认路径为 `data/GAIA/all.json` 与 `data/HLE/all_500.json`，也可通过 `--dataset` 指定其他 JSON 文件。

或在 Docker 中挂载本目录：

```powershell
docker run -it --rm -v ${PWD}:/workspace -v ${PWD}/data:/workspace/data run-agent
```

## 体积说明

当前本地完整子集约 **70 MB**（含 GAIA 附件与 HLE 图片），已由 `.gitignore` 排除。若只跑文本题，可只准备元数据 JSON。
