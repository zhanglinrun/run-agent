# P2 收口记录：事务存储、组合提交与快照

本文件收口 P2-3、P2-4、P2-6、P2-8 与 S01/S02/S03/S05/S06/S07/S08。

## 1. P2-4 的精确边界（不得含糊表述）

**只有最后一条最终 assistant 消息与宿主终态在同一事务中提交。**

具体而言：

- 运行中的最终 assistant 消息被暂存（`session.py` 的 `_completion_entries`），不立即落库；
- 结束时由 `RunOutcome` 带上这些暂存条目，经 `complete_in_transaction` 与 `executions`、
  `sessions` 以及（网关路径下）`gateway_tasks` 终态和 result Outbox **在同一事务**提交；
- 提交失败时**不会**发出完成回执：`tests/redesign/test_coding_application.py::test_completion_failure_is_atomic_and_never_emits_settled`
  注入 `outcome_updated` 故障后，断言没有 AssistantMessage 条目落库且执行仍为 `running`。

**同时必须承认另一半**：工具结果与 custom entries 仍然独立提交（`_append_session_batch`），
且当后续消息到来时，先前暂存的 final 会被提前落库（此时它已成为中间事件）。
这是计划 §7.4 的既定设计——"中间已完成事件持续提交；最终事件通过明确的提交回执交给宿主
协调"——**不是缺陷**。

因此本收口的表述是：**最后一条最终事件与宿主终态同事务；中间事件按设计独立提交。**
不得写成"最终事件一律不独立写入"。

## 2. 组合提交点（P2-3）

一个具名单元 `UnitOfWork`（`src/run_agent_coding/storage/unit_of_work.py`）按声明顺序在**单个写事务**内
应用 `CommitParticipant`。两个跨领域组合点都改走它：

| 站点 | 参与者（顺序即原内联顺序） |
| --- | --- |
| `src/run_agent_gateway/repository.py` `GatewayRepository.complete` | `gateway-qualification` → `session-outcome` → `task-terminal` → `result-delivery` → `steering-requeue` |
| `src/run_agent_coding/storage/host.py` `SqliteHostServices.publish` | `extension-validation` → `extension-rebind` → `task-interruption` → `activation-entry` |

契约测试（`tests/redesign/test_unit_of_work.py`）：参与者按序应用、任一抛错则此前的写入全部回滚、
失败后可重新提交、空提交为 no-op。

单元放在 **Coding 侧**：计划 §3.3 禁止 Coding 的 storage 反向导入 Gateway，而 Gateway 可以导入
Coding。已用 grep 确认 `src/run_agent_coding/storage/` 下无任何 `run_agent_gateway` 引用。

## 3. 快照（P2-6）

**回退路径就是主路径。** 会话状态始终由 `SessionState.from_entries` 从原始事件重建；模型输入
快照是派生证据，不是恢复用的加速结构。因此"快照缺失时从事件重建"无需实现。

已固化的不变量：

- 全历史重建在快照的 leaf 上与快照记录的消息、资源引用一致（S03）；
- `builder_version` 不兼容仍能恢复出相同历史与固定 Skill 版本；
- 快照无法解码不阻塞 resume；
- 共享上下文块被篡改时，下一次快照写入被拒绝（真正的篡改检测）。

被外键阻止、因而**不可达**的情形：`executions.snapshot_id` 引用 `context_snapshots`，支撑过执行的
快照不允许被孤儿化。这是设计如此，不是缺口。

## 4. 长历史测量（P2-8）

`scripts/measure_storage.py`（可重复运行）产出 `storage-measurements.json`。与它替代的 JSONL 存储
的基线（`baseline-storage.json`）对照：

| 事件数 | JSONL 单次追加 | SQLite 单次追加 | JSONL 预热读 | SQLite 分页读 |
| --- | --- | --- | --- | --- |
| 1,000 | 11.1—12.2 ms | 0.49—0.74 ms | 10.8 ms | 6.4 ms |
| 10,000 | 76.0—111.8 ms | 0.47—3.98 ms | 102.2 ms | 65.9 ms |
| 100,000 | 1166.2—1328.7 ms | 0.76—38.37 ms | 894.0 ms | 699.6 ms |

100,000 事件下单次追加从约 1.2 秒降到毫秒级，来源是消除了每次追加的全历史扫描——这是结构性
改善，不是常数优化。

`storage-measurements.json` 还记录了：批量填充、分支 fork、**新建连接的首次读取**（OS 页缓存
已热，**不是**冷缓存测量），以及**写入负载下的事件循环延迟分布**（约 1kHz 采样，报 max/p95/mean
与样本数，而不是单一数字）。p95 在 1000/10000/100000 下分别约 6.2/15.1/15.0 ms。

**边界**：这是单机、单进程、Windows 本地文件系统的合成历史测量。它不代表真实模型或工具性能，
也不能当作端到端 Agent 延迟。测量脚本与参数在仓库内，可重复运行。

## 5. 结构债

`src/run_agent_gateway/repository.py` 1196 行（最长函数 `admit()` 226 行）、
`src/run_agent_coding/storage/host.py` 409 行（`publish()` 110 行），均在我改动前就远超
200 行/30 行的限制。本次只把内联闭包改写为具名参与者，未做文件拆分；拆分记为独立重构。
新增的 `unit_of_work.py` 为 46 行、最长函数 10 行，`measure_storage.py` 约 200 行内。
