# Hermes 对齐落地状态

本文件记录 `docs/implementation/hermes-alignment.md` 中每一条的**实际落地情况**。
区分「已上线」「机制就绪未接线」两类，避免把测试通过误读为功能生效。

## 已上线

| 条目 | 实现 | 证据 |
|---|---|---|
| USER/MEMORY **字符预算**（非 token） | `extensions/experience/curation.py` | `tests/redesign/test_experience_curation.py`（9 测试） |
| 超限**回传用量**而非静默截断 | `CharacterBudget.reserve` / `BudgetExceeded.usage` | 同上 |
| **stale 30 天 / archive 90 天**时间转移 | `curate()` | 同上 |
| **pinned 保护** | `curate(pinned=True)` 恒为 KEEP | 同上 |
| dry-run 可判定（同决策、`applied=False`） | `CurationDecision.applied` | 同上 |
| 写来源 provenance（只有 review 分支写的才可被自动维护） | `src/run_agent_coding/host/learning.py` | `tests/redesign/test_skill_provenance.py`（5 测试） |
| 评测期禁止学习写回 | `require_writeback` / `writeback_disabled` | `tests/redesign/test_learning_writeback.py`（3 测试） |

## 机制就绪，**未接线**（不得读作已实现）

### 1. 复盘让路前台

机制：`ForegroundGate.deferral()` —— 测试覆盖「忙则延后、闲则不延后、活读不缓存」。

**未接线原因**：应用在 `prompt` 返回后**仍报 `is_running=True`**，若用它作为前台信号，
**每一次复盘都会被延后**——即"一个 run 看起来被复盘了，而实际上什么都没复盘"。

这个错误被实测抓到两次：
- 放在**触发器**上 → 完成事件在会话仍在运行时到达 → 全部延后
- 放在**消费层**但喂 `is_running` → `test_review_pipeline` 当场变红

**当前状态**：`ReviewCoordinator._gate` 显式默认 `busy=lambda: False`，即**机制在位但关闭**。
**未决问题**：什么信号才真正表示"用户的 run 在飞行中"。

### 2. 复盘用量归属父运行

机制：`ReviewLedger(parent_run_id=...)` + `attribution()`；未归属则抛 `UnattributedUsage`
（匿名花费无法解释，拒绝上报而非上报匿名数据）。

**未接线原因**：复盘目前**尚未执行模型工作** —— `ReviewCoordinator._consume_once`
只做入队与声称，不调用模型。**没有花费，就没有可归属的东西。**

因此该机制是为复盘真正执行模型工作时准备的，管道尚不存在，不硬接。

## 结论

Hermes 对齐中**可离线验证的部分已全部上线**（字符预算、时间转移、pinned、provenance、
评测期写回禁用）。剩余的**互斥与用量归因**是机制已实现并测试、但**依赖尚未存在的
运行时信号与模型调用**，故不声称已实现。
