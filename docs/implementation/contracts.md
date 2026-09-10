# 新版契约草案（P0-2）

本文件把 `study/简历五条/00-RunAgent完整改进执行计划.md` §5.2/§5.4/§5.5/§7 里
**拟议**的产品语义，对照当前源码固定成可评审草案。每条都带 `file:line`，凡与计划不一致
之处都显式标为偏差，不粉饰。本文件只记录事实，不修改运行时代码。

**路径约定**：本文所有引用都写相对仓库根目录的完整路径，不用裸文件名。
仓库里同时存在 `src/run_agent_gateway/schema.sql`（Gateway schema）与
`src/run_agent_coding/storage/schema.sql`（共享库 schema），以及三个不同的 `contracts.py`，
裸文件名会产生歧义。

## 1. 身份字段

| 字段 | 含义 | 产生位置 | 稳定性 |
| --- | --- | --- | --- |
| `adapter_instance_id` | 渠道适配器实例身份 | `RouteIdentity` `src/run_agent_gateway/contracts.py:16` | 随 Adapter 实例 |
| `account_id` / `chat_id` / `thread_id` / `subject_id` | 结构化路由字段 | `RouteIdentity` `src/run_agent_gateway/contracts.py:17-20` | 结构化，非裸字符串拼接 |
| `principal_id` | 已验证的用户/服务主体 | `Submission.principal_id` `src/run_agent_gateway/contracts.py:26` | 长期稳定 |
| `source_message_id` | 渠道消息去重键 | `Submission.source_message_id` `src/run_agent_gateway/contracts.py:27` | 由渠道给出，不由消息文本生成 |
| `task_id` | 一次被持久接收的逻辑任务 | `AdmissionReceipt.task_id` `src/run_agent_gateway/contracts.py:37` | 重试不重新创建 |
| `run_id` | **一次执行尝试** | 每次 claim 新生成 `uuid4().hex`，`src/run_agent_gateway/repository.py:730` | **每次尝试都变** |
| `attempt` | 该任务的第几次尝试 | `src/run_agent_gateway/repository.py:731` | 随 claim 递增 |
| `generation` | 执行资格栅栏 | 随 claim +1 `src/run_agent_gateway/repository.py:731`；取消时也 +1 `src/run_agent_gateway/repository.py:1113` | 随 claim 与取消递增 |
| `conversation_epoch` | 路由绑定切换代数 | `gateway_routes.epoch` `src/run_agent_gateway/schema.sql:16`；`AdmissionReceipt.conversation_epoch` `src/run_agent_gateway/contracts.py:39` | `/new` 时 +1 |
| `workspace_id` | 工作区身份 | `Assignment.workspace_id` `src/run_agent_gateway/contracts.py:103` | 参与租约 |
| `owner_id` | Gateway 宿主所有者 | `GatewayOwner.owner_id` `src/run_agent_gateway/contracts.py:90` | 重启后更换 |
| `sequence` | 会话内持久接收顺序 | `AdmissionReceipt.sequence` `src/run_agent_gateway/contracts.py:40` | 单调 |
| `RunToken` | 写入资格令牌 | `src/run_agent_core/session/contracts.py:21-25`（`session_id`/`owner_id`/`run_id`/`generation`） | 每次尝试 |
| `CompletionReceipt` | 持久完成回执 | `src/run_agent_core/session/contracts.py:66-73` | 每次运行一条 |

**已核实的关键语义**：`run_id` 是**每次尝试**的身份，不是逻辑任务身份。
`gateway_attempts` 以 `run_id` 为主键（`src/run_agent_gateway/schema.sql:109`），
`gateway_tasks.run_id` 只保存当前尝试；逻辑任务由 `task_id` 标识。这与计划 §5.2 一致。

**偏差 G4**：计划 §5.2 说 `run_generation` "执行资格变化时增加"。实际 `generation` 在
**每次 claim** 就 +1（`src/run_agent_gateway/repository.py:731`），取消时再 +1
（`src/run_agent_gateway/repository.py:1113`）。即它是"每次执行尝试都会换"的栅栏，而不是
只在资格变化时变。文档与测试必须按实际语义写。

## 2. 任务状态机

真实取值来自 SQLite CHECK 约束 `src/run_agent_gateway/schema.sql:65`：

```text
queued | steering | consumed | running | cancelling | cancelled
       | succeeded | failed | interrupted | outcome_unknown | blocked
```

| 转移 | 触发 | 位置 |
| --- | --- | --- |
| → `queued` | 持久准入（普通任务，或 steer 没有 target run） | `src/run_agent_gateway/repository.py:471`、`:518` |
| `queued` → `steering` | 忙时 steer 绑定到当前运行的 run | `src/run_agent_gateway/repository.py:471` |
| `steering` → `consumed` | 在下一个可消费边界消费：追加 `gateway.steering` CustomMessage | 写入 `src/run_agent_gateway/repository.py:959`；事务入口 `:980` |
| `steering` → `queued` | 目标 run 在消费前结束，**同事务**原子转回队列 | `src/run_agent_gateway/repository.py:896` `_queue_unconsumed_input` |
| `queued` → `running` | claim 到执行槽与工作区 | `src/run_agent_gateway/repository.py:733` |
| `running` → `cancelling` | 停止意图 | `src/run_agent_gateway/repository.py:1113` |
| `cancelling` → `cancelled` | 收敛完成 | 取消路径 |
| `running` → `succeeded`/`failed`/`interrupted` | 最终事务提交 | `src/run_agent_gateway/repository.py:894` |
| → `outcome_unknown` | 宿主死亡且外部副作用不明 | `src/run_agent_gateway/repository.py:208` `acquire_owner` |
| → `blocked` | **无写入者** | 见缺口 G2 |

计划的 `queued -> running -> succeeded/failed -> cancelling -> cancelled ->
interrupted/outcome_unknown` 是子集；实际多出 `steering`、`consumed`、`blocked` 三个状态。
`rejected` 不是任务状态而是准入结果（`AdmissionRejected`
`src/run_agent_gateway/contracts.py:109`），与计划一致。

`steering` → `queued` 的回退发生在 `complete` 的同一事务内，并带一个 `control` Outbox
记录（reason: `Target run ended before consuming this input`）以及故障点
`gateway_steering_requeued`（`src/run_agent_gateway/repository.py:920`）。这正是 G03 要求的
"消费或转队列恰有一个确定结果"。

**缺口 G1**：`gateway_attempts.status`（`src/run_agent_gateway/schema.sql:115`）
**没有 CHECK 约束**，尝试级状态集未在 schema 中固定，而计划 §5.4 要求"任务状态与尝试状态
分开"。当前只能在代码里找取值。

**缺口 G2**：`blocked` 已被 CHECK 约束允许（`src/run_agent_gateway/schema.sql:65`），也被
claim 的前驱依赖检查读取（`src/run_agent_gateway/repository.py:700`
`p.status IN ('queued','steering','blocked')`），但 `git grep` 显示整个 Gateway 包中
**没有任何地方写入 `blocked`**。它目前是只读不写的状态：既没有进入条件，也没有离开条件。

## 3. 投递、工作区与控制状态

| 对象 | 取值 | 位置 |
| --- | --- | --- |
| `gateway_outbox.status` | `pending \| sending \| sent \| failed` | `src/run_agent_gateway/schema.sql:131` |
| `gateway_outbox.kind` | `accepted \| result \| control` | `src/run_agent_gateway/schema.sql:127` |
| `gateway_workspaces.status` | `available \| leased \| quarantined` | `src/run_agent_gateway/schema.sql:42` |
| `gateway_controls.state` | `waiting \| completed` | `src/run_agent_gateway/schema.sql:91` |
| `RunStatus` | `succeeded \| failed \| cancelled \| interrupted \| outcome_unknown` | `src/run_agent_core/session/contracts.py:51` |

去重与冲突：`gateway_inbox` 主键 `(adapter_instance_id, source_message_id)`
（`src/run_agent_gateway/schema.sql:104`），并保存 `payload_hash`（同文件 `:99`）用于
"同键不同内容报冲突"；同一行只能指向 task 或 control 之一
（`CHECK((task_id IS NULL) != (control_id IS NULL))`，同文件 `:105`）。

## 4. Schema 版本与失败行为

- 共享库：`SCHEMA_VERSION = 8`（`src/run_agent_coding/storage/sqlite.py:16`），打开时用
  `PRAGMA user_version` 与 `APPLICATION_ID` 校验，不匹配即报错
  （`src/run_agent_coding/storage/sqlite.py:126`）。
- Gateway：版本 5 存在 `host_metadata` 的 `gateway.schema` 键，不匹配抛
  `ValueError("Unsupported Gateway schema")`（`src/run_agent_gateway/repository.py:75-80`）。
- 两者都**没有旧版本转换链**，与计划 §7.9"遇到未知版本明确报错，不猜测转换"一致。

## 5. 事务边界（组合提交点）

只有跨领域、必须原子的位置才算组合提交点。以下为实际存在的位置：

| 组合提交点 | 同事务写入内容 | 位置 |
| --- | --- | --- |
| 准入 | `gateway_tasks` + `gateway_inbox` + `gateway_routes` + accepted Outbox + 后台会话克隆 | `src/run_agent_gateway/repository.py:518` |
| 最终提交 | 会话最终事件 + `gateway_tasks` 终态 + artifacts + result Outbox | `src/run_agent_gateway/repository.py:894` |
| steer 消费 | `gateway.steering` 事件追加 + 状态改 `consumed` + result Outbox | `src/run_agent_gateway/repository.py:980` |
| 会话完成（内层） | 令牌校验 + 追加事件 + `executions` + `sessions` | `src/run_agent_coding/storage/sessions.py:437` → `:303` |
| 扩展发布 | `extension_owners` 重绑 + 激活事件追加 | `src/run_agent_coding/storage/host.py:167` |
| 取消 | 状态与 `generation` + `execution_revocations` + Outbox | `src/run_agent_gateway/repository.py:1113` |
| 恢复放行 | 进程指纹 + 工作区 + 进程日志 + result Outbox | `src/run_agent_gateway/recovery.py:257` |
| 所有者接管 | owner + 孤儿标记 + 撤销 + 输入重排队 | `src/run_agent_gateway/repository.py:208` |
| 资源 CAS 批量 | 多个 state 变更 + head 变更 | `src/run_agent_coding/storage/state.py:224` |
| 分支 | 新分支头 + 事件 | `src/run_agent_coding/storage/sessions.py:751` |

共 24 处 `write=True` 事务入口在 `src/run_agent_gateway/*.py`，25 处在
`src/run_agent_coding/storage/*.py`（按文件：`repository.py` 17、`sessions.py` 10、
`outbox.py` 3、`host.py` 3、`state.py` 3、`telemetry.py` 3、`controller.py` 2、
`recovery.py` 2、`tasks.py` 2，另有 `handle.py`/`processes.py`/`resources.py`/
`skill_packages.py` 各 1）。数字由 `git grep -n "write=True"` 统计。
**其中只有上表 10 处是跨领域的**；其余是单领域事务。

**缺口 G3**：计划 §7.4 要求"在组合层由共享 UnitOfWork 协调会话 repository 和 Gateway
repository"。实际"最终提交"是 `repository.complete` 内部的一个闭包
（`src/run_agent_gateway/repository.py:828`），没有具名可复用的 UnitOfWork，且
`src/run_agent_coding/storage/host.py:167` 是第二处同构组合点。这属于 P2-3，已由 T-015 覆盖。

## 6. 配额

`GatewayLimits`（`src/run_agent_gateway/contracts.py:45-56`）实测值：

```text
waiting_total=1024  waiting_foreground_reserved=256  waiting_background_reserved=64
per_session=16      per_principal=128                 background_roots_per_principal=4
payload_bytes=65536 outbox_pending=4096
running_total=8     running_foreground_reserved=4     running_background_reserved=1
```

计划的"待执行总上限 1,024、前台保留 256、后台保留 64、每 Session 最多 16"完全一致；
共享 = 1024-256-64 = 704，执行共享 = 8-4-1 = 3，也与计划一致。计划未提及的额外约束是
`per_principal=128`、`background_roots_per_principal=4`、`payload_bytes=65536`、
`outbox_pending=4096`。

## 7. 待收口缺口

| 编号 | 缺口 | 归属 |
| --- | --- | --- |
| G1 | `gateway_attempts.status` 无 CHECK，尝试级状态未固定 | 记录；P3-5/P3-8 适用时收口 |
| G2 | `blocked` 状态只读不写：无进入与离开条件 | 记录；P3 适用时收口 |
| G3 | 无具名 UnitOfWork，组合提交是内联闭包 | T-015 |
| G4 | `generation` 的实际递增时机与计划表述不同 | 已如实记录，不需改代码 |

本文只固定语义。上述缺口均**没有**在本次被修，避免把 P0-2 扩张成实现任务。
