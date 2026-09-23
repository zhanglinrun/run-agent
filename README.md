# Run Agent

用于研究和验证长任务恢复、分层上下文压缩、动态扩展一致性与经验发布门禁的本地 Python Coding Agent Harness。项目参考 Pi 的小核心与扩展思想，但不把“Python 重写”视为创新；重点研究四个可故障注入的运行时问题：动态扩展的一致采用、分支会话恢复、扩展拥有并受提交契约约束的分层上下文压缩，以及发布前验证的 Skill 演进。

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

Provider、Core、Coding 三层通过同步 `setup(api)` 装配七个内置扩展：`experience`（Skill 演进）、`memory`（USER.md / MEMORY.md 记忆）、`compaction`（分层压缩策略）、`curator`（技能库维护）、`mcp`、`permission_policy` 和 `plan_mode`。每个注册带来源和 generation；setup 半途失败会按来源撤销注册。记忆、压缩与策展都是可选扩展：`--no-extensions` 关闭全部，`--extension <name-or-path>` 按短名或路径单独装载。

`/reload`、`/new`、`/resume` 和分支替换采用 staged runtime：候选完成源码校验、资源准备和 host publication 前，旧 runtime 保持 active；publication 成功后旧 generation 才进入只读 retiring 阶段，收到 shutdown 通知并逆序执行 disposer。发布失败不会提前关闭旧 MCP 连接或清除旧 UI 状态。

该机制只管理宿主注册、任务和显式 disposer，不是 OS 沙箱，也不能撤销扩展自行完成的文件或网络副作用。

### 2. DAG 会话与原子 JSONL

每场会话保存在 `<cwd>/.run/sessions/<id>.jsonl`。消息和资源条目由 `id/parent_id` 组成 DAG；最后一条 `leaf` 决定活动分支。`/rewind` 只追加新 leaf，旧枝不删除；`/fork` 复制根到目标节点的有效路径，包括资源激活、compaction 和 run commit。

存储层在同一个跨进程独占锁内完成 `compare_and_append(expected_head)`：比较活动 head、验证 parent、计算序号并提交。单条记录尾追加并 `fsync`；多条 batch 使用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。两个 writer 竞争同一 head 时只有一个成功。

每个完成的 run 追加 `RunCommitEntry`，固定 start/end、状态、snapshot 和错误；后台能力读取指定 run 时不会看到后续回合。Session index 是 append-only last-write-wins 日志，并在阈值后锁内压缩。

### 3. 分层上下文压缩（compaction 扩展）

压缩唯一由内置 `compaction` 扩展（`src/run_agent_extensions/layered_compaction`，my-pi-agent 的 cheap-first 四层移植，溯源 Pi 的 `compaction.ts`）承担，加载即拥有压缩：

```text
gate 未超 80% 预算 -> 一条层都不跑，原样返回
gate 超预算 -> L1 大结果落盘（cwd/.run/tool-results/<tool_call_id>.txt）
            -> L2 视图过长时裁中间轮（头 3 + 尾 46 + 1 条占位）
            -> L3 旧工具结果占位（保留最近 5 条）
            -> 重新估算：仍超预算才跑 L4（一次有界推理生成摘要，保留 user 边界尾段）
```

层号按**执行顺序**编排；参考实现按语义编号（其 `L3`/`L1`/`L2` 分别是本实现的 L1/L2/L3，摘要层两边都是 `L4`），对照时注意这处差异。

记忆不是压缩层：`MEMORY.md` / `USER.md` 永远不会被当成摘要。记忆扩展在压缩前产生的洞察只作为素材，经 `session_before_compact` 闸门带回、包在 `<memory-provider-context>` 里进入摘要提示词，并明确标注“只当素材、不当指令”。

扩展在 `before_provider_request` 里改写请求，并通过 `session_compact_request` 请求提交；core 校验该请求并写入单条持久 `CompactionEntry`（连同 `LeafEntry`）。扩展还注册 `/compact [instructions]`，并带 413/context-overflow 的 reactive 路径。

core 侧不再做任何压缩：没有自动阈值、没有摘要生成、没有大结果落盘。每个 **agent 侧** Provider 请求都由 core 按顺序执行「扩展改写 → 测量 token → 超窗拒绝」：扩展改写先跑，然后测量；只要 `tokens > context_window_tokens`，就在物理 I/O 之前抛 `ContextBudgetExceeded`。扩展自己的推理通道（L4 摘要、curator 复核）不经过这条路径。最终 View 与 `tokens_before`/`tokens_after`/`stable_prefix_digest` 随模型输入快照记录，完整 JSONL 历史不修改。

未加载扩展时（例如 `--no-extensions`）没有任何压缩，只剩上述硬窗口守卫。

### 4. 扩展化记忆与 Verifier-Gated Skill 演进

记忆归内置 `memory` 扩展（`src/run_agent_extensions/hermes_memory`，hermes-agent 移植）：

- `USER.md`：用户偏好（用户作用域，`~/.run/USER.md`）；
- `MEMORY.md`：项目事实（项目作用域，`<cwd>/.run/MEMORY.md`）；
- `memory` 工具与 `/memory show|add|replace|remove [--scope]` 在原子写、字符预算和威胁检查下编辑这两个文件；写入立即落盘，但提示里的快照在 `session_start`/`/reload` 冻结，保证前缀缓存稳定。项目未受信时项目作用域不注入也不接受写入。记忆与 session history 不同层：压缩不读记忆文件，记忆也不会被压缩——它只在压缩前把 provider 洞察交给摘要提示词当素材。

```text
/memory show
/memory add <user|memory> <content>
/memory replace <user|memory> <old_text> <new_content>
/memory remove <user|memory> <old_text> [--scope project|user]
```

Skill 演进仍归 `experience` 扩展，保留 `SKILL.md` 这一种本地资产：模型不能直接修改正式 Skill；`skill_manage propose` 只能基于持久 `RunCommitEntry` 产生 cold candidate。候选保存在 loader 不可见的 `experience/candidates/`，内容按 SHA-256 存储，并绑定 source run、base/candidate digest、有界文本操作和项目 probe。

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

技能库维护归内置 `curator` 扩展（`src/run_agent_extensions/curator`，hermes 技能策展移植）：它不在正常会话里改内容，只按周期门控运行——`session_start` 先给首次会话只记录 `last_run_at`，之后按 `CURATOR_INTERVAL_HOURS`（默认 168 小时）才维护一次：闲置技能标记 `stale`（默认 30 天），无保护且 `created_by: evolution` 的闲置技能整目录归档到 `<skills-root>/.archive/`（默认 90 天；只移动不删除，user/project 资产默认拒绝自动归档），改动前对每个技能根做整库快照（`CURATOR_BACKUP_KEEP` 默认保留 5 份）。`/curator restore <snapshot-id|archived-skill>` 整库回滚或恢复单个归档，`/curator journey list|show|delete` 提供技能与记忆的统一视图。有界 LLM 复核只发送一次元数据请求且只产候选：consolidation 走 `SkillEvolution.propose` 生成 cold candidate，技能内容仍只能过 `/evolve` 的 verifier 与发布门禁。归档、恢复、暂停等破坏性动作需要 UI 确认，非交互时被拒绝。

受控演进任务位于 `evals/evolution/`。最终报告严格区分 train、selection 和 test；test 不参与发布决策，结果不外推到任务族之外。

## 分层

| 层 | 职责 |
| --- | --- |
| `run_agent_ai` | OpenAI-compatible / Anthropic 协议、流式响应、重试和用量 |
| `run_agent_core` | 消息、Agent Loop、工具事务、取消和通用会话协议 |
| `run_agent_coding` | CodingSession、TUI/CLI、扩展宿主、Context Budget 守卫和 JSONL 会话树 |

Observability 与 Evals 是横向证据模块。默认编码工具为 `read`、`write`、`edit`、`bash`；扩展可额外提供工具和策略。项目资源仅在通过 trust gate 后加载。当前生命周期管理不构成操作系统权限隔离。

## 常用会话命令

`/new`、`/resume`、`/tree`、`/branch <entry-id>`、`/rewind <entry-id>`、`/fork <entry-id>`、`/name`、`/model`、`/thinking`、`/compact [instructions]`（由 `compaction` 扩展注册）、`/reload` 和 `/export` 走同一 `CodingApplication` 生命周期。旧会话固定保存的 Skill 和扩展源码版本；要采用当前资源，显式使用 `--refresh-resources`。

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
