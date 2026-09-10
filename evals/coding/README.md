# Coding task fixtures

## 首选：任务目录布局

`tasks/<id>/` 是新的任务布局，支持独立验收与参考解准入：

```text
tasks/<id>/
  task.toml        # id、version、kind、budget_seconds、artifacts；grader.command
  instruction.md   # 交给 Agent 的用户可见输入
  environment/     # 固定初始环境（会复制进 Agent 工作区）
  grader/          # 独立验收资产，执行阶段不可用
  reference/       # 参考解，用于任务准入检查
```

`task.toml` 里的 `artifacts` 是**唯一**决定哪些产物进入评分的字段。评分目录由
原始 `environment/`、声明的 artifacts 和任务自带 `grader/` 组成，工作区里的其它内容一律
不带入——所以改写可见测试、放 `conftest.py` 忽略收集、或改 `pytest.ini` 都无法影响判分。

`tasks.json` 记录任务选择清单（含 family 与 tags）；只有 `status: "ready"` 的条目有磁盘目录。

任务准入检查（no-op 必须失败、参考解必须通过、纯文字答复必须失败）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/redesign/test_real_task_specs.py -q
```

## 兼容：单文件清单

`tasks.jsonl` 每行定义一个隔离任务：

```json
{"id":"python-off-by-one","fixture":"fixtures/python-off-by-one","prompt":"修复边界条件并确保测试通过。","verify":[["python","-m","pytest","-q"]],"tags":["bug-fix","python"]}
```

字段：

- `id`：campaign 内唯一任务标识。
- `fixture`：相对清单的只读种子目录；每个 trial 会复制到独立临时工作区。
- `prompt`：交给标准 `CodingSession` 的任务。
- `verify`：模型执行结束后运行的确定性验收命令列表。
- `timeout_seconds`：单条 verifier 超时，默认 120 秒。
- `tags`：仅用于任务分类。

注意：这种单文件清单的 `verify` 在工作区内运行，因此它**不是**独立验收，只适合验证链路。

运行两个故意以失败状态开局的 smoke fixture：

```powershell
.\.venv\Scripts\run.exe bench run evals/coding/smoke/tasks.jsonl `
  --output-root .run/evals/coding-smoke `
  --extension extensions/observability `
  --candidate-id smoke
.\.venv\Scripts\run.exe bench rebuild .run/evals/coding-smoke
```

正式简历指标应扩大并冻结任务集，保留相同 task id 的 baseline/candidate 配对；当前 smoke
仅用于验证复制、Agent 修改、外部 verifier、调用账本、trace 与离线重建的端到端链路。
