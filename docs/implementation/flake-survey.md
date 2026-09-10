# 间歇用例调查与抑制存量登记

配套工具：`scripts/flake_survey.py`（可重复跑全量并汇总失败分布）。

## 一、为什么要先建机制

闸门今天红过三次，**三次是不同的用例**，且每个在隔离下都通过：

| # | 用例 | 状态 |
|---|---|---|
| 1 | `test_terminal::test_secret_dialog_does_not_pollute_history_and_timeout_restores_editor` | ✅ **已修**（根因经证实，见本文件前几节） |
| 2 | `test_process_supervisor::test_coroutine_cancellation_waits_for_real_descendants` | ❌ 未定性 |
| 3 | `test_mixed_load::test_one_session_replays_the_mixed_foreground_background_control_script` | ❌ **已实测排除"超时"** |
| 4 | `test_process_supervisor::test_successful_root_cannot_leave_detached_child` | ❌ 未定性 |

逐个追是在回答错误的问题。缺的是**分布**：哪些用例失败、多频繁、失败集合是否稳定。
`scripts/flake_survey.py` 就是为此而建：重复跑并把失败映射到用例名。

## 二、调查结果（当前）

```
$ python scripts/flake_survey.py 8          # 直跑 pytest
  pass 1..8: 76.0–79.2s  417 passed each
  === result ===  passes: 8   no failures observed in any pass

$ # 6 轮真正的 verify.py
pass 1 exit=0 :: All 10 steps passed.
...
pass 6 exit=0 :: All 10 steps passed.
```

**14 次连续全绿**（8 次直跑 + 6 次闸门）。所以当前无法复现失败。

诚实结论：
- 那 3 次失败**成簇出现**，不是稳定速率；今日早先"1/3"的印象来自一个小样本
- 不能因此宣称"已修复"——未定性的 3 个用例（#2/#3/#4）**仍然未定性**
- 但 criterion 1 的前提现在有 14 次连续见证，且失败时 `eventually()` 会自报耗时、
  预算与被等待的检查名，下一次复现将给出可行动证据

## 三、抑制存量登记（criterion 9 口径）

criterion 9 要求交付面无 ruff 抑制。事实如下，逐条可核验：

| 范围 | 抑制数 | 说明 |
|---|---|---|
| **本轮新增的 12 个模块与全部测试** | **0** | 见下方扫描命令 |
| **本轮触碰过的文件** | **0** | `run_agent_evals/runner.py` 原有的 2 处已真正修掉 |
| 交付面其余部分 | 44 | **全部前序遗留**，`git blame` 归到 2026-09-04 的提交 |

扫描命令：

```
$ Get-ChildItem src,extensions,tests,evals -Recurse -File -Include *.py |
    Select-String -Pattern 'noqa|type: ignore|pragma: no cover|breakpoint\(\)|pdb\.set_trace|# TODO|# FIXME'
```

保留的 44 处**大多是有意的隔离边界**，例如：

- `extensions/runtime.py:381  # noqa: BLE001 - extensions are an isolation boundary`
- `update_check.py:130          # noqa: BLE001 - update checks must never block startup`
- `loop.py:697                 # noqa: BLE001 - tools are an isolation boundary`

**不删除它们的理由**：收窄这些捕获会**改变核心子系统（`session.py`、`loop.py`、
extension runtime）的 fail-safe 行为**，属于 P6 范围之外，且每处都需要独立测试。
把它们声称成"不存在"是不实陈述，所以此处作为**已知存量**登记。

`run_agent_evals/runner.py` 的两处修法可作范本：

- 盲捕 → 拆成 `ExecutionFailure`（保留部分结果）与
  `(OSError, ValueError, KeyError, RuntimeError)`（本边界预期的失败类），
  **其余异常照常抛出**——比原先更好：未知失败是缺陷，不是任务结果
- `# type: ignore[arg-type]` → `status: TrialStatus = "error"`，类型由声明保证
