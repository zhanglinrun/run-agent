# 检查点 04：扩展宿主服务与受管理任务

日期：2026-09-10。完整改造仍在执行。

- CodingApplication 注入 SQLite HostServices，不依赖 Gateway。扩展使用固定 source_id 和宿主绑定的 session/project/user 作用域访问状态 CAS、不可变资源与产物。
- 复用 ExtensionGeneration 的 UUID 身份，不增加并行世代计数。整组扩展的写入资格在一个事务中替换；旧实例在 API 调用和数据库提交时都失效。失败 setup 的子守卫共享同一身份，但可以单独失效，防止捕获的 API 重新注册。
- reload 先准备注册和资源，再提交服务绑定、发布内存实例，最后调用新 session_start。提交前失败保留旧实例，提交点取消完成一致切换。原同步 reset_for_reload 路径已删除。
- 扩展命令由 application 异步执行，支持等待 SQLite 操作；解析过程只返回执行意图，不把协程作为消息返回。
- 受管理本地任务注册命名处理器，SQLite 保存 JSON 描述、源代际和结果。默认最多执行 2 个、接收 32 个未完成任务；排队不创建等待协程。支持状态、取消、关闭收敛和新进程标记 interrupted，不自动重放。
- 取消未收敛的任务保留为 cancelling 并报告，不宣称已停止；旧任务无法提交资源头。注册回滚清除任务处理器。
- 观测扩展增加 /trace export 和 /trace status，使用相同任务、产物和作用域服务生成报告。

验证：75 项新版测试通过，mypy 检查 146 个源文件通过，Ruff 通过。wheel/sdist 构建和新环境安装通过，源码目录外验证会话、SQLite 观测、HostServices 和 reload。

证据：host-services-tests.xml、distribution-check-04.json。接口说明：host-services.md。数据库从空库直接初始化 schema 4，不提供旧版迁移或兼容。

仍待完成：真正的资源/上下文快照和延迟 Skill 版本固定、任务快照校验、可选评测服务与复盘预算、Gateway 持久调度/Outbox、experience 与独立评测。TaskSpec 的 snapshot_id 目前只保存引用字段，尚不代表资源已经固定。
