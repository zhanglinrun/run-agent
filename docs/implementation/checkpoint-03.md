# 检查点 03：SQLite 观测与正式评测路径

日期：2026-09-10。完整改造仍在执行；本检查点不代表 Gateway、经验学习或真实模型评测完成。

- 诊断、ProviderCallLedger、TraceRecorder 均使用宿主注入的 SQLite sink；产品 Python 代码中的 JSONL reader/writer、诊断路径及旧导出已移除。数据库直接初始化 schema 3，不提供旧版迁移。
- 可选轨迹使用有界批量队列，flush 保存丢弃计数；费用记录等待数据库提交。落库失败显式报告，已提交的物理尝试仍可读取，未完成账本不能作为完整费用。
- CodingApplication 注入 Provider 装配钩子，首次创建、切换、刷新、命名和压缩继续计入同一账本。装饰器保留模型窗口发现与资源关闭。
- CodingTaskExecutor 使用正式 application 和持久 SQLite 会话；模型失败、取消携带输出、调用记录与费用进入 TrialArtifact。未知费用与已知费用分别记录，未终结的逻辑调用保留在统计分母中。
- 调用与 HTTP 尝试携带 root_id，辅助调用沿用会话归属；普通轨迹通过 source_id 和加载代际隔离。
- 轨迹基准使用 SQLite 并将 durable flush 纳入计时；归档前关闭数据库，分别报告逻辑记录大小和数据库大小。

验证：63 项 tests/redesign 用例通过，mypy 检查 144 个源文件通过，Ruff 通过。测试覆盖成功、模型错误、取消、满载、落库失败、分页、费用保留、模型切换、命名、压缩和 reload。wheel/sdist 构建成功，全新安装在源码目录外通过 run 路由、会话完成/恢复、SQLite 观测和无 JSONL 输出检查。

证据：telemetry-tests.xml、distribution-check-03.json。测试使用离线 Provider / HTTP 传输，不代表真实模型任务成功率或运行性能。

待补：完整根任务预算、独立 grader、多轮/学习评测及消融，HostServices、受管理任务和资源快照，持久 Gateway 与 experience 发布闭环。
