# Run Agent

用于研究和验证长任务恢复、上下文虚拟化、动态扩展一致性与经验发布门禁的本地 Python Coding Agent Harness。项目参考 Pi 的小核心与扩展思想，但不把“Python 重写”视为创新；重点研究四个可故障注入的运行时问题：动态扩展的一致采用、分支会话恢复、协议感知的上下文虚拟化，以及发布前验证的 Skill 演进。

Run Agent 不是 Claude Code 或 Codex 的产品替代品。成熟产品更适合日常编码；本项目提供可替换、可观测、可测试的 Harness 策略，用于解释一次长任务如何执行、恢复、压缩和积累经验。

## 安装与启动

要求 Python 3.12+：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\Activate.ps1
run --help
```

按 [.env.example](.env.example) 配置 OpenAI-compatible 或 Anthropic Provider。环境中的配置优先于项目 `.env`；`--provider`、`--model`、`--thinking` 只覆盖本次运行。

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
run bench --help
```

管道输入必须明确使用 `--print`。业务结果走 stdout，诊断走 stderr。默认 Textual TUI 支持 Markdown 流式消息、工具进度、会话/分支选择、模型切换和运行中 steer/follow-up。

## 四条技术主线

### 1. 事务化扩展生命周期

Provider、Core、Coding 三层通过同步 `setup(api)` 装配 MCP、权限、计划模式和 Experience。每个注册带来源和 generation；setup 半途失败会按来源撤销注册。

`/reload`、`/new`、`/resume` 和分支替换采用 staged runtime：候选完成源码校验、资源准备和 host publication 前，旧 runtime 保持 active；publication 成功后旧 generation 才进入只读 retiring 阶段，收到 shutdown 通知并逆序执行 disposer。发布失败不会提前关闭旧 MCP 连接或清除旧 UI 状态。

该机制只管理宿主注册、任务和显式 disposer，不是 OS 沙箱，也不能撤销扩展自行完成的文件或网络副作用。

### 2. DAG 会话与原子 JSONL

每场会话保存在 `<cwd>/.run/sessions/<id>.jsonl`。消息和资源条目由 `id/parent_id` 组成 DAG；最后一条 `leaf` 决定活动分支。`/rewind` 只追加新 leaf，旧枝不删除；`/fork` 复制根到目标节点的有效路径，包括资源激活、compaction 和 run commit。

存储层在同一个跨进程独占锁内完成 `compare_and_append(expected_head)`：比较活动 head、验证 parent、计算序号并提交。单条记录尾追加并 `fsync`；多条 batch 使用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。两个 writer 竞争同一 head 时只有一个成功。

每个完成的 run 追加 `RunCommitEntry`，固定 start/end、状态、snapshot 和错误；后台能力读取指定 run 时不会看到后续回合。Session index 是 append-only last-write-wins 日志，并在阈值后锁内压缩。

### 3. Cheap-First 上下文虚拟化

持久会话历史和实际 Provider View 分离。超过预算时按以下顺序生成 detached view：

```text
L3 大 ToolResult 按 SHA-256 内容寻址落盘
 -> L1 按完整 Turn / ToolCall 组折叠中间历史
 -> L2 旧 ToolResult 替换为带 digest 的短引用
 -> L4 仍超预算才生成持久结构化摘要
```

所有裁切保持 `Assistant(tool_calls) + ToolResult*` 配对；模型给出的 call ID 不参与文件路径。L1-L3 足以满足预算时不调用摘要模型；免费层后仍超过硬窗口则在 Provider I/O 前拒绝请求。最终 View 及每层 token/artifact 报告随模型输入快照记录，完整 JSONL 历史不修改。

确定性基准及离线校验：

```powershell
run bench context --output-root .run/benchmarks/context
run bench context-rebuild .run/benchmarks/context
```

基准数字只描述冻结的合成长历史，不代表真实模型成功率或账单成本。

### 4. Verifier-Gated Experience

Experience 仍是普通 Session 扩展，保留三种本地资产：

- `USER.md`：用户偏好；
- `MEMORY.md`：项目事实；
- `SKILL.md`：可复用程序性知识。

Memory 可通过 `memory` 工具受控更新。模型不能直接修改正式 Skill；`skill_manage propose` 只能基于持久 `RunCommitEntry` 产生 cold candidate。候选保存在 loader 不可见的 `experience/candidates/`，内容按 SHA-256 存储，并绑定 source run、base/candidate digest、有界文本操作和项目 probe。

候选只有在 host-owned `EvaluationService` 对 baseline/candidate 使用相同模型、工具、预算和隐藏 grader 配对运行后才能发布。任何 baseline 已通过任务回退、基础设施错误、base digest 漂移、probe 漂移、user-owned/pinned 资产都拒绝发布。没有 evaluator 时保持 cold，模型自评和使用次数不能代替 verifier。

```text
/evolve status
/evolve candidates
/evolve show <candidate-id>
/evolve adopt <skill>
/evolve publish <candidate-id>
/evolve reject <candidate-id> <reason>
/evolve ledger [skill]
/evolve rollback <ledger-id>
```

受控演进任务位于 `evals/evolution/`。最终报告严格区分 train、selection 和 test；test 不参与发布决策，结果不外推到任务族之外。

## 分层

| 层 | 职责 |
| --- | --- |
| `run_agent_ai` | OpenAI-compatible / Anthropic 协议、流式响应、重试和用量 |
| `run_agent_core` | 消息、Agent Loop、工具事务、取消和通用会话协议 |
| `run_agent_coding` | CodingSession、TUI/CLI、扩展宿主、Context View 和 JSONL 会话树 |

Observability 与 Evals 是横向证据模块。默认编码工具为 `read`、`write`、`edit`、`bash`；扩展可额外提供工具和策略。项目资源仅在通过 trust gate 后加载。当前生命周期管理不构成操作系统权限隔离。

## 常用会话命令

`/new`、`/resume`、`/tree`、`/branch <entry-id>`、`/rewind <entry-id>`、`/fork <entry-id>`、`/name`、`/model`、`/thinking`、`/compact`、`/reload` 和 `/export` 走同一 `CodingApplication` 生命周期。旧会话固定保存的 Skill 和扩展源码版本；要采用当前资源，显式使用 `--refresh-resources`。

## 开发验证

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m ruff check src scripts tests
.venv\Scripts\python.exe scripts/verify.py
.venv\Scripts\run.exe bench suite evals/coding/tasks
```

组件测试证明协议和故障边界，不证明真实模型能力。真实模型结论必须附任务版本、模型配置、Skill digest、失败分母和可离线重建的 evidence；没有报告支持的百分比不进入项目描述。

旧版本升级与兼容行为（旧 Session/记忆/Skill 直读、旧 `.ledger.jsonl` 查看与 rollback、扩展源码变更后的 `--refresh-resources`、v2 备份恢复与 `~/.run/gateway` 数据）见 [MIGRATION.md](MIGRATION.md)。
