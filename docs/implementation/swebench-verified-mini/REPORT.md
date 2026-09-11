# SWE-bench Verified Mini — 实测报告

> 本文件由 `acb8991` 及其后续提交固化。每个数字都可从同目录的证据重算。

## 1. 设置

| 项 | 值 |
|---|---|
| 数据集 | SWE-bench Verified Mini（HAL 同款 50 题子集） |
| 实例 | 50（`django/django` 25，`sphinx-doc/sphinx` 25） |
| 模型 | `gpt-5.6-luna`，`thinking=max` |
| 评分 | 官方 `swebench` 5.0.2，**每题独立容器**（`swebench/sweb.eval.x86_64.*`） |
| Agent 可见 | **仅 problem statement** —— 不含 gold patch、不含 test_patch |
| 采样 | 每题 1 次（故 `Pass^k` 不可计算） |

**隔离方式**：agent 在 `repo@base_commit` 的 git worktree 上改代码；评分树由
`pristine environment + agent 声明的 artifacts + grader 自带测试` 重建。
**改写可见测试一分也拿不到** —— 官方容器会覆盖为真实 `test_patch`。

## 2. 结果

```
Pass@1                    : 36/50 = 72.0%
  django/django           : 18/25 = 72.0%
  sphinx-doc/sphinx       : 18/25 = 72.0%
SE = sqrt(p(1-p)/n)       : 6.35%
95% 置信区间 (Wald)        : [59.6%, 84.4%]
```

## 3. 成本

| | 准确率 | 总成本 | 每题 | 每解出 |
|---|---|---|---|---|
| SWE-Agent + Claude Sonnet 4.5 High（HAL 榜首） | 72.00% | $463.90 | $9.278 | $12.886 |
| **本次** | **72.0%** | **$8.23** | **$0.165** | **$0.229** |

**相同准确率，成本低 56.4 倍。**

> ⚠️ **成本来源是操作者侧供应商账单，不是本系统的账本。**
> 本系统账本上报 `$0.0000` —— 端点未给 `gpt-5.6-luna` 定价，
> 每条消息 `cost.total = 0.0`。**这必须读作「不可定价」，不能读作「免费」。**
> 这是账本的一个真实缺口：`ledger.py` 在**聚合层**已经设计成「无成功则 `cost_per_success=None`」，
> 但**源头**给出的 0.0 会被照抄。

## 4. 用量

```
消息数                 : 1,271
input tokens           : 3,612,910
output tokens          : 234,172
reasoning tokens       : 87,535
cached tokens          : 34,357,248
平均每题 input         : 72,258
单题耗时 p50 / p95     : 253s / 607s
等效单价               : $2.28 / 1M input, $35.15 / 1M output
```

## 5. 最重要的发现：28% 的「自述成功但未解题」

```
agent 自述 succeeded      : 50/50
官方判定 resolved         : 36/50
自述成功但未解题          : 14/50 = 28.0%
误报失败（解出却说失败）  : 0/50        ← 单向偏置
```

> **若采信 agent 自述，本次分数会是 100%，而不是 72%。**

这就是第 7 章「完成度与逻辑错误 / 症状修复与验证造假」被量化后的样子，
也是「**为什么评分必须放在 agent 够不到的地方**」这个设计的实测价值。
5 题试点时该比例是 40%，50 题时是 28%。

## 6. 局限（必须与结果同时陈述）

1. **72.0% 与 60% 或 84% 不可区分** —— `n=50` 的 95% CI 是 `[59.6%, 84.4%]`，宽 12.4pp。
   与榜首**同数只是数值巧合，不是等价性结论**。
2. **django 与 sphinx 各 18/25 完全相同** —— `n=25` 时半区间 CI 约 ±17pp，两者互不可区分。
   这是巧合，不是发现。
3. **`Pass^k` 不可计算** —— 每题只跑 1 次。第 7 章强调关键场景优先 `Pass^k`。
4. **单模型、单 Harness** —— 没有模型替换实验，无法区分「瓶颈在模型」还是「在 Harness」。
5. **仅 2 个仓库** —— 该子集只含 django 与 sphinx，不能外推到其他语言或仓库。

## 7. 本次运行暴露的三个自身缺陷

| 缺陷 | 表现 | 修复 |
|---|---|---|
| `git worktree add` 四路并发静默失败 25 题 | stderr 只有 git 进度行，**异常信息看起来像成功信息** | 按 repo 串行 + 重试（救回 2 题 `rc=128`） |
| 官方 harness 在中文 Windows 用 GBK 写日志 | 3 题 `UnicodeEncodeError: 'gbk' codec` | `PYTHONUTF8=1` |
| 进程挂交互会话下 | 拉完 46/50 镜像后被杀 | 改用计划任务解耦 |

## 8. 复现

```powershell
# 证据在同目录；重跑评分（不花模型钱）
E:\Anaconda\python.exe -m swebench.harness.run_evaluation `
  --dataset_name SWE-bench/SWE-bench_Verified `
  --predictions_path <由 patches/ 重建的 predictions.jsonl> `
  --run_id verify --max_workers 8
```

`patches/` 内含 50 个 patch，`inventory.json` 内含每个文件的 sha256。
先 `docker pull` 对应镜像 —— **官方 harness 自己不会拉取**。
