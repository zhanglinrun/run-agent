# 扩展宿主服务

HostServices 由 CodingApplication 的 SessionManager 注入，不启动 Gateway。`setup(api)` 声明工具、命令、事件和命名任务处理器；Session 发布扩展写入资格后，回调通过 `context.services` 使用服务。斜杠命令通过 `context.api.context.services` 访问同一对象，可以返回文本或异步返回文本。

```python
from run_agent_coding.host.contracts import StateChange, TaskSpec


def setup(api):
    async def summarize(payload, task):
        state = task.services.scope("project").state
        previous = await state.get("summary")
        await state.compare_and_set(
            StateChange("summary", previous.version if previous else 0, payload)
        )
        return {"saved": True}

    async def command(args, context):
        return await context.api.context.services.tasks.submit(TaskSpec("summarize", args))

    api.register_task_handler("summarize", summarize)
    api.register_command("summarize", command, description="Queue a project summary")
```

服务范围只能选择 `session`、`project`、`user`。真实身份由宿主生成，状态和资源同时绑定扩展 source_id；模型参数不能传入任意用户或项目。`scope()` 返回状态 CAS、不可变资源/资源头和产物接口。产物按内容哈希保存，引用检查仍要求属于当前扩展作用域。这些是宿主生命周期和数据访问契约，不构成不可信 Python 代码沙箱。

本地任务池默认最多 2 个执行任务，最多接收 32 个未完成任务，描述和结果各最多 64 KiB。SQLite 保存处理器名称、JSON 输入、源实例和结果；排队任务不保存协程，也不创建等待协程。TaskSpec 的快照引用字段已预留，实际资源快照校验仍待接入。

`services.tasks.status(task_id)` 查询任务，`cancel(task_id)` 请求取消。会话替换、reload 和关闭撤销旧服务的写入资格并取消任务。未在收敛期限内退出的任务保持 `cancelling`，保留句柄并报告；不会把请求取消等同于已停止。进程重启后旧本地任务记为 `interrupted`，不自动重放。持久 Gateway 的恢复执行尚未接入此本地实现。

观测扩展是第二个消费者：`/trace export` 提交报告导出任务，`/trace status <id>` 查询结果。报告包含跨度及汇总，保存为受作用域约束的产物，不产生 JSONL。

reload 先准备新注册与资源，再以一个 SQLite 事务替换整组写入资格；随后发布新内存实例并调用 session_start。提交前失败保留原实例；提交边界收到取消时完成一致的切换。旧接口在内存和数据库提交时都会被检查。
