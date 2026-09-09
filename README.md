# Run Agent

面向多轮编码任务的本地 Agent Harness，采用 Provider / Core / Coding / Gateway 四层，以及通过 `setup(api)` 装配的 Session 扩展。MCP、计划模式、权限和验证由扩展提供；Gateway 是独立宿主，渠道通过 `setup_gateway(api)` 接入。

项目正在按 [完整改进计划](study/简历五条/00-RunAgent完整改进执行计划.md) 实施改造。[执行记录](docs/implementation/README.md) 列出已验证范围与尚未完成的任务。持久网关调度、experience、独立评测与真实模型实验仍在实施，不能把组件测试成绩视为这些能力已完成。

## 安装与启动

要求 Python 3.12+。在仓库中安装后，激活虚拟环境即可在任意项目目录直接使用 `run`。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\Activate.ps1
run --help
```

按 [.env.example](.env.example) 配置模型凭据；项目 `.env` 会在启动时读取，现有进程环境变量优先。例如设置 `OPENAI_API_KEY`，按需配置 `OPENAI_BASE_URL`，并通过 `--provider` / `--model` 选择模型。

```text
run
run "分析这个仓库"
run --session <session-id>
run --print "解释这段代码"
run --print --mode json "解释这段代码"
run --sessions
run --providers
run gateway --help
run bench --help
```

管道输入必须明确使用 `--print`；JSON 模式输出一个完整结果文档，包含 `run_id`、`session_id`、`branch_id`、`status`、`head_id`、`watermark` 和 `text`。业务结果走 stdout，诊断走 stderr。`--` 可分隔以保留子命令或选项开头的提示词。

交互模式保留终端滚动记录：Enter 发送，Alt+Enter 换行，Ctrl+C 停止当前操作，Ctrl+D 退出。运行中输入普通文本会排入纠正队列，`/queue <内容>` 排入后续回合；`/expand <tool-call-id>` 展开工具输出。`/help` 查看会话命令。

## 会话与存储

交互、print 和 Gateway 会话池使用共享的 Coding 应用生命周期和 SQLite 会话实现。默认数据库为 `~/.run/state.sqlite3`；`--state-dir <目录>` 可指定独立状态目录。

- 数据库工作线程执行有界请求与短事务；会话历史按序号分页读取。
- 消息批次与分支头一起提交；创建分支及其摘要可原子完成。
- 最终答案、执行结果与完成水位在同一事务中提交，完成回执只在提交之后发出。
- 每轮写入具有所有者与世代资格；完成后失效，迟到回调不能继续提交到已结束的运行。
- `/new`、`/resume`、`/tree`、`/branch <entry-id>`、`/name`、`/model`、`/thinking`、`/compact`、`/reload` 通过统一应用路径处理。
- `/export` 生成可阅读的 HTML；新版数据库备份与恢复由 SQLite 一致性快照和产物清单承担。

新版会话不读取、迁移或写出旧格式，不保留旧命令和 Textual 组件 API。旧版本开发状态不作为新版恢复输入。目前观测与评测的部分旧文件存储仍待替换，详见执行记录。

## 分层与扩展

| 层 | 职责 |
| --- | --- |
| Provider：`run_agent_ai` | 模型适配、流式响应、请求重试与用量 |
| Core：`run_agent_core` | 消息、推理循环、工具、取消和通用会话协议 |
| Coding：`run_agent_coding` | 编码会话、统一终端、Skills、Compaction、扩展宿主与 SQLite |
| Gateway：`run_agent_gateway` | 渠道生命周期、会话路由及多会话运行宿主 |

Observability 与 Evals 是横向配套模块。默认工具为 `read`、`write`、`edit`、`bash`。可选扩展位于 [extensions](extensions)，通过 `run --extension <路径>` 加载；也可安装到 `~/.run/extensions`。项目扩展发现使用 `--project-extensions`，项目资源是否可信由已有信任策略决定。

扩展 UI 提供文本通知、选择、确认、输入与 `context.ui.set_status(key, text)`，不接管终端控件。重载会使旧扩展 API 失效，并清理其状态显示。该生命周期机制不构成操作系统沙箱。

## 开发验证

```powershell
.venv\Scripts\python.exe -m pytest tests/redesign -q
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m ruff check src extensions scripts tests/redesign
uv build
```

新版测试涵盖事务回滚、所有权失效、取消期间的资格交接、会话恢复与分支、终端编辑和进程级 print / 管道行为。模型服务可使用本地模拟端点；模拟结果不用于宣称真实模型效果或吞吐。
