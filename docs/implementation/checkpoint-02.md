# 检查点 02：SQLite 会话与统一终端

日期：2026-09-10。此记录是完整计划的阶段交付，不表示网关、experience 或评测改造完成。

已经接通：

- Core 的存储契约改为会话绑定句柄、分页读取、分支头、带资格的批量追加、分叉与最终完成回执。删除旧会话编码器和文件存储。
- SessionManager 使用异步 SQLite metadata / owner 操作；CodingSession、交互、print 与 Gateway 会话池直接使用新版存储，不提供旧 API 包装。
- 最终答案与执行结果在一个事务中提交，提交成功后发出 agent_settled。完成事务撤销本轮 token；重复完成必须匹配完整原始内容。宿主可注入组合式 outcome committer。
- 分叉与摘要同事务提交；初始创建、恢复、命名、分叉和实际 read 工具调用已通过行为测试。
- 取消期间先确认短事务结果，再归还句柄与资格；模型生成器关闭后提交取消状态。恢复时把没有完成回执的旧 attempt 记录为 outcome_unknown，不自动重放工具。
- 统一 CodingApplication 与 Rich / prompt_toolkit 终端，删除旧 TUI、Textual 依赖、RPC 和逐事件 JSON 呈现。run --print --mode json 返回单个完整结果，run --session 恢复 SQLite 会话。
- 扩展 UI 改为文本状态和异步对话；旧组件 API 删除。状态按 source_id 归属，重载清理状态并拒绝旧 API 写入。秘密输入隐藏且不进入输入历史。
- 修复 Responses 传输提前结束却形成成功空结果的问题：缺少终结事件会失败。

证据：

- `tests/redesign/test_coding_application.py`：事务回执、恢复和分叉、最终提交回滚、取消与生成器关闭、取消后资格交接、恢复未知结果、真实文件读取工具。
- `tests/redesign/test_terminal.py`：多行编辑、终端退出、Ctrl+C、对话超时、秘密输入、扩展状态与重载。
- `tests/redesign/test_print_sqlite.py`：独立进程、源码目录外启动、本地 HTTP 模型响应、管道、单个 JSON、跨进程恢复、截断响应失败。
- `application-tests.xml`：新版测试集结果。
- `distribution-check-02.json`：全新虚拟环境中的 wheel 安装、唯一 run.exe、命令路由、应用完成与恢复。安装环境没有 Textual。

验证范围与后续工作：

- 模型是本地模拟端点或可控 Provider，工具读取实际临时文件；尚无新的真实模型效果或吞吐成绩。
- Gateway 仍使用原调度器，持久 Inbox、任务控制器、Outbox、容量预留、工作区隔离和实际子进程取消尚未落地。会话原子完成接口不等于 Outbox 已实现。
- HostServices 的实际注入、受管理扩展任务和固定资源版本仍待完成，agent_settled 的 snapshot_id 将随资源快照补齐。
- 会话没有 JSONL，但观测、诊断和评测仍有旧文件路径；整个产品无 JSONL 的 S08 验收未完成。
- SQLite 冷启动仍显式加载历史缓存；上下文快照与长历史优化另行验收。
- 新终端测试验证输入行为；跨平台真实终端视觉检查、真实进程取消和发行默认交互的完整演示仍待补齐。

不需要也不实现旧数据、旧配置或旧扩展 API 的兼容与迁移。当前 schema 为 2，测试从空状态目录初始化。
