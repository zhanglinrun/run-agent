# Run Agent

面向多轮编码任务的本地 Agent Harness，采用 Provider / Core / Coding / Gateway 四层，以及通过 `setup(api)` 装配的 Session 扩展。MCP、计划模式、权限和经验记忆由扩展提供；Gateway 是把同一个 Agent 接到飞书的独立宿主。

组件测试验证实现契约，真实模型效果以具体评测配置与报告为准。使用与配置说明见 [文档索引](src/run_agent_coding/data/docs/README.md)。

## 安装与启动

要求 Python 3.12+。在仓库中安装后，激活虚拟环境即可在任意项目目录直接使用 `run`。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\Activate.ps1
run --help
```

按 [.env.example](.env.example) 配置模型：只支持 OpenAI 兼容和 Anthropic 两种协议，端点、密钥、默认模型和思考强度都由环境变量给出（`OPENAI_*` / `ANTHROPIC_*`、`MODEL`、`REASONING_EFFORT`）。项目 `.env` 会在启动时读取，现有进程环境变量优先；`--provider` / `--model` / `--thinking` 只覆盖本次运行。

```text
run
run --no-tui
run "分析这个仓库"
run --session <session-id>
run --session <session-id> --refresh-resources
run --print "解释这段代码"
run --print --mode json "解释这段代码"
run --sessions
run --providers
run gateway --help
run bench --help
```

管道输入必须明确使用 `--print`；JSON 模式输出一个完整结果文档，包含 `run_id`、`session_id`、`branch_id`、`status`、`head_id`、`watermark` 和 `text`。业务结果走 stdout，诊断走 stderr。`--` 可分隔以保留子命令或选项开头的提示词。

`run` 默认打开基于 Textual 的全屏聊天界面：消息区可滚动查看完整历史，回答以 Markdown 实时呈现，工具输出可展开查看。独立的 `run_agent_coding.tui` 模块参考 Tau 的 TUI 组织方式，负责界面、输入和事件展示，继续使用现有 CodingApplication 与 SQLite 会话。使用 `run --no-tui` 可切回原有终端界面；`--print` 与 JSON 输出不受影响。

输入 `/` 浏览命令，Tab 补全，Ctrl+P 打开命令选择器；`/model`、`/resume` 和 `/tree` 打开可搜索的选择窗口。Enter 发送（有补全选项时先接受补全），Alt+Enter 或 Ctrl+J 换行，Ctrl+C 停止当前操作，输入框为空时 Ctrl+D 退出。`/theme` 切换主题，`/sidebar` 或 Ctrl+B 显示/隐藏侧栏，Ctrl+E 展开/收起全部工具输出。运行中输入普通文本会在下一个工具边界用于纠正当前任务，`/queue <内容>` 排入后续回合；`/help` 查看会话命令。

## 会话与存储

交互、print 和 Gateway 使用共享的 Coding 应用生命周期和 SQLite 会话实现。默认数据库为 `~/.run/state.sqlite3`；`--state-dir <目录>` 可指定独立状态目录。

- 数据库工作线程执行有界请求与短事务；会话历史按序号分页读取。
- 消息批次与分支头一起提交；创建分支及其摘要可原子完成。
- 最终答案、执行结果与完成水位在同一事务中提交，完成回执只在提交之后发出。
- 每轮写入具有所有者与世代资格；完成后失效，迟到回调不能继续提交到已结束的运行。
- `/new`、`/resume`、`/tree`、`/branch <entry-id>`、`/name`、`/model`、`/thinking`、`/compact`、`/reload` 通过统一应用路径处理。
- `/export` 生成可阅读的 HTML；新版数据库备份与恢复由 SQLite 一致性快照和产物清单承担。

调用账本、轨迹和评测执行记录也写入 SQLite；评测的清单、报告与产物保留为可复核文件。

会话恢复固定已保存的 Skill、扩展资源内容与版本，并校验扩展源码及工具入口。资源发布不自动改变当前会话输入；`/reload` 或恢复时显式指定 `--refresh-resources` 才采用当前资源，并记录新的激活事件。后台会话固定来源快照，不能使用该刷新选项。

## Gateway

`run gateway` 把同一个编码 Agent 接到飞书上，结构参考 hermes-agent 的网关：飞书适配器把消息整理成统一事件并按聊天串行处理；会话键由聊天、话题和发送者决定，键到会话 ID 的映射保存在 `~/.run/gateway/sessions.json`；每个活跃聊天保留一个已打开的 Agent，闲置一段时间后关闭。目前只实现飞书一个渠道。

飞书连接与网关策略来自环境变量：`FEISHU_APP_ID`、`FEISHU_APP_SECRET` 是必填项，`FEISHU_ALLOWED_USERS` 列出允许对话的 open_id，`FEISHU_REQUIRE_MENTION` 控制群聊是否必须 @ 机器人，`GATEWAY_SESSION_RESET_MODE` 决定会话是否按闲置或每日重置。当前仅支持 WebSocket 长连接和文本消息，不支持 webhook、媒体输入、流式回复或工具进度推送。配置说明见 [.env.example](.env.example) 和 [CLI 文档](src/run_agent_coding/data/docs/cli.md)。

```powershell
.venv\Scripts\python.exe -m pip install -e ".[feishu]"
run gateway --cwd . --state-dir .run
```

聊天命令：`/new`（或 `/reset`）开始新会话，`/stop` 停止当前任务，`/status` 查看状态，`/heartbeat add <分钟> <提示词>` 创建定时唤醒，`/help` 查看说明，`/model`、`/thinking`、`/compact` 与终端里一致。一个聊天同一时刻只跑一轮；`GATEWAY_BUSY_INPUT_MODE` 默认为 `interrupt`，新消息中止当前任务后开始新一轮；设为 `queue` 时逐条排队，设为 `steer` 时尝试纠正当前任务，无法注入时排队。队列默认最多 32 条，满时明确提示重试。默认情况下，未授权的私聊发送者会收到自己的 open_id 提示，群聊里则直接忽略。

长期运行相关：每轮在解析后的会话 ID 上取一次回合租约，两个聊天映射到同一会话时按序执行而不是交错写同一份历史；回复在发送前先记入投递账本，进程崩溃后重启会把未确认送达的回复补发（首次尝试可能已到达的会带上明显的补发标记）；心跳任务持久化在 `~/.run/gateway/heartbeats.json`，到点后以内部消息唤醒会话。默认连续 300 秒无事件会提醒一次，由用户决定是否 `/stop`；无事件提醒本身不会终止任务。

## 分层与扩展

| 层 | 职责 |
| --- | --- |
| Provider：`run_agent_ai` | 模型适配、流式响应、请求重试与用量 |
| Core：`run_agent_core` | 消息、推理循环、工具、取消和通用会话协议 |
| Coding：`run_agent_coding` | 编码会话、统一终端、Skills、Compaction、扩展宿主与 SQLite |
| Gateway：`run_agent_gateway` | 飞书适配器、聊天到会话的路由和按聊天缓存的 Agent |

Observability 与 Evals 是横向配套模块。默认工具为 `read`、`write`、`edit`、`bash`，新建终端、print 和 Gateway 会话默认加载全部四个内置扩展：`experience`、`mcp`、`permission_policy`、`plan_mode`。经验扩展提供 `memory`、`skill_manage`、自动复盘与 Skill 生命周期维护；权限策略默认 `guarded`，计划模式初始关闭，使用 `/plan on` 开启只读规划。MCP 扩展已加载不代表已连接服务：未配置端点时服务数为零，不提供外部 MCP 工具。事件追踪由会话自带，`run --trace` 打开后用 `/trace` 查看。内置扩展位于 [src/run_agent_extensions](src/run_agent_extensions)。终端和 Gateway 均支持 `--no-extensions` 禁用默认及自动发现的扩展，显式 `--extension <名称或路径>` 仍有效且重复路径只加载一次；用户扩展可安装到 `~/.run/extensions`。项目扩展发现使用 `--project-extensions`，项目资源是否可信由已有信任策略决定。

经验分别保存在 `~/.run/USER.md`、项目 `.run/MEMORY.md` 和 Skill 目录中；项目记忆与 Skill 读写仍受项目信任限制，仅对可信项目使用 `--trust-project`。旧会话保持原有扩展快照；保留历史并采用全部默认扩展时，终端使用 `run --session <id> --refresh-resources`，网关使用 `run gateway --cwd . --refresh-resources`。自动复盘会额外调用模型并可能写入经验，细节与独立开关见 [Experience 说明](src/run_agent_extensions/experience/README.md)。

扩展 UI 提供文本通知、选择、确认、输入与 `context.ui.set_status(key, text)`，不接管终端控件。重载会使旧扩展 API 失效，并清理其状态显示。该生命周期机制不构成操作系统沙箱。

扩展可通过 `register_resource_provider` 从宿主提供的只读资源视图中选择上下文；资源视图按来源与 session / project / user 作用域隔离，激活前没有状态写入和任务提交能力。

## 开发验证

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m ruff check src scripts tests
.venv\Scripts\python.exe scripts/verify.py
```

新版测试涵盖事务回滚、所有权失效、取消期间的资格交接、会话恢复与分支、终端编辑和进程级 print / 管道行为。模型服务可使用本地模拟端点；模拟结果不用于宣称真实模型效果或吞吐。
