# Hermes 对齐落地状态

本文件记录 `docs/implementation/hermes-alignment.md` 中每一条的**实际落地情况**。

## 已上线

| 条目 | 实现 | 证据 |
|---|---|---|
| USER/MEMORY **字符预算**（非 token） | `extensions/experience/curation.py` | `tests/redesign/test_experience_curation.py`（9 测试） |
| 超限**回传用量**而非静默截断 | `CharacterBudget.reserve` / `BudgetExceeded.usage` | 同上 |
| **stale 30 天 / archive 90 天**时间转移 | `curate()` | 同上 |
| **pinned 保护** | `curate(pinned=True)` 恒为 KEEP | 同上 |
| dry-run 可判定（同决策、`applied=False`） | `CurationDecision.applied` | 同上 |
| **复盘让路前台** | `ForegroundGate` + `ReviewCoordinator._gate` | `tests/redesign/test_review_yields_to_foreground.py`（3 测试） |
| **复盘用量归属父运行** | `ReviewLedger(parent_run_id=...)` + 消费路径构造并回报 | `tests/redesign/test_review_usage_attribution.py`；`tests/redesign/test_review_pipeline.py` 仍绿 |
| 写来源 provenance | `src/run_agent_coding/host/learning.py` | `tests/redesign/test_skill_provenance.py`（5 测试） |
| 评测期禁止学习写回 | `require_writeback` / `writeback_disabled` | `tests/redesign/test_learning_writeback.py`（3 测试） |

### 前台信号是**实测**选定的，不是推断

用探针在真实应用上量测 `ExtensionContext.is_running`：

```
before start : False
after start  : False
after prompt : False        （失败 provider 与成功 provider 都是 False）
```

即它的语义是"有 run 在飞行中"，正是互斥需要的信号。

## 需要更正的一段记录

本文件先前的版本写着"应用在 prompt 返回后仍报 `is_running=True`，故该信号不可用"，
**这是错的**，证据在上述实测中。

那次误判的来源是一次**我自己造成的失败**：`ForegroundGate` 当时漏了 `@dataclass`，
于是 `ForegroundGate(busy=...)` 抛 `TypeError`，协调器构造失败，`test_review_pipeline`
因此变红。我把**自己的类型错误**误读成"信号选错了"，并在提交信息与文档里写下了那个结论。

教训与做法一致：**先量测，再下结论**。更正后接通 `is_running`，24 项复盘相关测试全通过。

## 用量归属当前的真实数值

`usage` 中的 `requests` / `input_tokens` 目前为 **0**，因为复盘**尚未真正执行模型工作**
（`consume` 入队、声称、回报用量，但不调用模型）。这是**如实的状态**：
ledger 已在生产路径中创建并绑定父运行，所以复盘将来任何花费**已经是可归属的**，
而不是一笔无人认领的匿名成本。
