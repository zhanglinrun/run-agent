# 检查点 07：Gateway 持久事务基础

日期：2026-09-10。完整改造继续执行；当前 run gateway 仍使用旧调度入口，本检查点不代表新网关已接通。

- Gateway 自己定义 schema.sql，由 repository 在共享 SQLite 的一个事务中初始化。当前主库 schema 6，Gateway 命名空间版本 1；只初始化当前版本，无迁移和兼容实现。
- 结构化路由区分 adapter/account/chat/thread/subject；会话使用独立 UUID。身份、路由、新会话、任务、Inbox 和 accepted Outbox 原子创建。重复来源 ID 返回原任务，同键异内容或跨主体访问拒绝。
- 准入同时检查总等待、分类保留、每会话、每主体、后台根任务和 payload 上限。为已接收任务的最终结果保留 Outbox 空间，避免结果提交时才发现投递积压无额度。
- claim_next 在一个事务中选取会话队首、按类别和会话轮转、检查保留容量并取得工作区。等待工作区的任务不占执行槽。Windows 工作区键规范化大小写。
- 逻辑 task 与 attempt 分开。任务终态不释放执行槽；release 需要 Runner 已实际退出这一宿主事实。取消中的任务保留槽和工作区。
- 最终 RunOutcome、会话事件、任务终态与 result Outbox 可在同一事务提交；故障点验证回滚，重复提交不改写原完成时间。已经有 Coding execution 的任务不能绕过会话完成接口。
- stop/cancel 先提交意图与执行撤销记录，旧 token 随即不能追加中间事件或正常成功结果；保持租约不恢复写资格。已撤销运行只接受不带迟到历史的 cancelled 完成回执。成功先提交时，随后 stop 不覆盖成功。
- Outbox 独立于 run_generation，固定 delivery_id，保存哈希、尝试、退避、发送声明与渠道回执。结果在 accepted 之后投递；同一回执可重试确认。旧 owner 不能确认新投递声明。
- owner 过期恢复把未核对执行标为 outcome_unknown，并隔离其工作区、撤销写资格；不会仅凭租约到期释放运行额度或重放工具。待投递消息可重新认领相同 delivery_id。

验证：18 项新增 Gateway 行为测试，包括真实 CodingApplication 的分配 run_id 与原子完成回执、数据库关闭/重开、并发去重、容量保留、工作区等待、终态与停止竞争、核心写撤销、事务故障和投递恢复。全套新版测试 111 项通过、1 项符号链接测试因 Windows 权限限制跳过；mypy 检查 153 个源文件、Ruff 通过。全新安装检查覆盖 Gateway schema、准入、完成与 Outbox；未调用真实模型或真实渠道。

证据：gateway-repository-tests.xml、distribution-check-07.json；安装环境 .run/redesign/install-env-07。

下一步必须接通实际执行：替换旧 scheduler/gateway/coding/pool 的执行路径，使用已验证的 Coding prompt run_id 和原子 committer 接口；控制器接通 status/steer/stop/cancel/new，steer 消费/转队列需要独立持久协议。Adapter 仍需身份映射、有界收发与生命周期，新后台任务仍需 Git worktree、固定上下文与产物。进程核对、人工恢复入口、过期清理和混合负载尚未完成。Repository 的 Submission 目前是可信宿主输入，不能直接接受未校验的渠道 payload 或任意后台工作区。
