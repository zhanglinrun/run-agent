# 迁移说明

本文件说明旧版本（gateway 与后台 review 控制环的时代）升级到当前 Harness 版本时会遇到的兼容行为。它只描述当前代码里真实成立的行为；升级没有自动数据迁移或清理步骤。

## 1. 会话、记忆与正式 Skill：直接读取

- 旧 Session JSONL（`<cwd>/.run/sessions/<id>.jsonl` 与同目录 `index.jsonl`）直接读取：消息与资源条目仍按 `id/parent_id` DAG 解析，`--sessions`、`--session <id>`、rewind/fork/branch 沿用原文件，不做格式转换。
- 旧 `USER.md`、`MEMORY.md` 直接读取，并按现有字符预算注入提示；`/memory` 与 `memory` 工具继续在原子写、预算和威胁检查下编辑这两个文件。记忆职责已从 `experience` 扩展迁到独立的内置扩展 `memory`（`src/run_agent_extensions/hermes_memory`）：文件路径与格式未变——`~/.run/{MEMORY,USER}.md`（用户作用域）与 `<cwd>/.run/{MEMORY,USER}.md`（项目作用域）继续直接读取，`§` 分隔的条目格式、字符预算、快照冻结和威胁屏蔽语义保持一致。
- 记忆相关环境变量改名：旧 `EXPERIENCE_MEMORY_*` / `EXPERIENCE_USER_*` 不再被读取，等价配置现为 `HERMES_MEMORY_*`（如 `HERMES_MEMORY_CHAR_LIMIT`、`HERMES_MEMORY_USER_CHAR_LIMIT`、`HERMES_MEMORY_ENABLED`、`HERMES_MEMORY_USER_PROFILE_ENABLED`、`HERMES_MEMORY_WRITE_APPROVAL`）。`EXPERIENCE_*` 仍保留 Skill 演进相关项（`EXPERIENCE_SKILLS_WRITE_APPROVAL`、`EXPERIENCE_SKILL_GUARD`、`EXPERIENCE_SKILL_LEDGER`、`EXPERIENCE_EVOLUTION_*`）。
- 正式 Skill（`<skills-root>/<name>/SKILL.md`）由标准 loader 直接发现并冻结进会话资源快照，不要求旧 frontmatter 具备新字段。

## 2. 旧 sidecar 与旧 ledger：保留、可查、可 rollback

- `.usage.json`、`.archive/`、`.ledger.jsonl` 及 ledger 的 `.blobs/` 不会被删除或改写（`src/run_agent_extensions/experience/skill_usage.py`）。旧 `.usage.json` 现在只读：`pinned` 仍是发布/采纳的 policy 输入（`skill_usage.py:55-56`、`skill_manager.py:189-190`），consultation 计数不再更新。内置 `curator` 扩展使用自己的用量日志 `<extension_state_dir>/curator/usage.jsonl`（用户级 state 目录），只把 lookback 窗口内的新 consultation 当作活动证据：旧 `.usage.json` 里的历史 consultation 计数不会恢复。
- 旧 ledger 条目照常加载和展示：读取只解析文件，不经过新条目的 actor 白名单（`skill_ledger.py:31-33`），所以 `/evolve ledger` 能看到退役控制环写下的历史条目；`/evolve rollback <ledger-id>` 仍能在 Skills 根内恢复 before-state，并在改动前追加一条 pre-rollback safety 记录（`skill_ledger.py:202-271`）。

## 3. ownership 与 provenance

- frontmatter 缺少 `created_by` 时按 `created_by: user` 处理（`skill_manager.py:151`），不会被当成 evolution 资产；只有显式 `/evolve adopt <name>` 才改成 `created_by: evolution` 并记 ledger（`skill_manager.py:212-249`）。
- 首次发布由 `publish_candidate` 在正式 ledger 写入 candidate/report/probe/source-session/source-run provenance（`skill_manager.py:315-329`）；user-owned、pinned 或 base digest 漂移的 Skill 拒绝自动演进。

## 4. Experience 源码哈希变化后的历史会话

- 历史会话保存的是扩展来源的哈希快照。磁盘上的扩展源码变化后，在旧会话里继续使用旧快照会失败并提示 `Extension source changed; explicitly reload before use`（`src/run_agent_coding/extensions/runtime.py:479`）：系统不会伪造一个旧扩展版本。
- 采用当前扩展实现并保留历史消息：

  ```powershell
  run --session <session-id> --refresh-resources
  ```

  `--refresh-resources` 必须与 `--session` 同时给出（`src/run_agent_coding/cli.py:81-82`）。

## 5. 备份

- 新备份只写 `run.backup.v3`，且只收集 `sessions/` 下的文件（`backup.py:47-58`、`84-88`）；v3 manifest 一旦声明 `sessions/` 之外的路径，校验阶段即拒绝（`backup.py:120-123`）。
- 旧 `run.backup.v2` 仍可 verify 与 restore（`backup.py:18`、`113`）；v2 manifest 允许包含 `gateway/sessions.jsonl`、`gateway/deliveries.jsonl` 这类旧状态文件，恢复会按清单原样复制（`tests/redesign/test_backup.py:68-79`）。
- restore 不覆盖已存在的目标目录（`backup.py:137-138`），目标目录里既有的旧状态文件不会被删除（`tests/redesign/test_backup.py:107-117`）。
- 备份/恢复目前是存储层 API（`create_backup` / `verify_backup` / `restore_backup`，见 `src/run_agent_coding/storage/backup.py`），CLI 没有备份子命令。

## 6. 磁盘上的 `~/.run/gateway` 数据

- 代码里已没有 gateway 子系统：`src/` 没有任何路径会读取、扫描或删除 `~/.run/gateway`，升级也不会搬运或清理这份数据。保留还是手动删除由用户决定。

## 7. 已删除的命令面与环境变量

- `run gateway`、`/review`，以及 `FEISHU_*`、`GATEWAY_*`、`EXPERIENCE_REVIEW_*`、`EXPERIENCE_CURATOR_*` 和旧 nudge 配置都不再被读取或提供。`/curator` 作为内置 `curator` 扩展回归——它是维护型扩展，命令面为 `/curator status|run [--dry-run]|pause|resume|restore <snapshot-id|archived-skill>|report|review|learn <description>|journey list|show <id>|delete <id>`；它只读新的 `CURATOR_*` 环境变量，`EXPERIENCE_CURATOR_*` 不会被读取。当前 Skill 演进命令面是 `/evolve status|candidates|show|adopt|publish|reject|ledger|rollback`。

## 8. core 侧压缩被移除：压缩归 `compaction` 扩展（当时包名 `claude_compaction`，现已更名，见第 11 节）

- `compaction.strategy` 与 `compaction.enabled` 不再被读取：`~/.run/settings.json` 或 `<cwd>/.run/settings.json` 里的遗留 `compaction` 键会被静默忽略（不再报错，也不再影响任何行为），`/session` 不再显示压缩阈值。
- core 不再做任何压缩：没有自动阈值判定、没有摘要模型调用、没有大结果内容寻址落盘（`.run/context/blobs/` 不再产生）。`/compact`、`CodingSession.compact` / `compact_detailed`、`ContextViewPipeline`（连同 `PreparedContext`、L1-L3 与 blob 诊断）都已删除；`tests/redesign/test_context_window.py` 等测试也随之改写。
- core 只保留不可绕过的守卫与提交通道：每次 Provider 请求先跑扩展的 `before_provider_request` 改写，再测量 token，超过 `context_window_tokens` 即在物理 I/O 之前抛 `ContextBudgetExceeded`（`run_agent_coding/context_budget.py` 的 `ContextBudgetGuard`，同时 `context_view.py` 改名为 `context_budget.py`）。
- 压缩唯一由内置 `compaction` 扩展（`src/run_agent_extensions/claude_compaction`）拥有：加载即拥有压缩（L1 snip → L2 microcompact → L3 LLM 摘要），开关只剩扩展自己的 `COMPACTION_LAYER_ENABLED` 与分层开关。扩展通过 `CompactionCommitRequest` 请求的提交仍由 core 校验并写成单条持久 `CompactionEntry`。
- 未加载扩展时（例如 `--no-extensions`）没有任何压缩：超窗请求会被 `ContextBudgetExceeded` 拒绝，而不是被截断或摘要。
- `run bench` 下的 `context` 与 `context-rebuild` 两个子命令已移除（离线策略对比基准），`src/run_agent_evals/context_bench.py` 随之删除。
- 上下文溢出后的自动重试不再存在：旧版本由 core 在本地注入摘要后重试同一请求，现在没有任何 core 侧重试；`AutoRetryStartEvent`/`AutoRetryEndEvent` 与 `CompactionStartEvent`/`CompactionEndEvent` 已从 `run_agent_coding/events.py` 及其 wire 联合中删除，`SessionAgentEndEvent.will_retry` 恒为 `False`。替代路径是扩展的 reactive 压缩，它在收到 413/context-overflow 之后于同会话的下一次请求生效。
- 上述「扩展改写 → 测量 → 超窗拒绝」只覆盖 agent 侧的 Provider 请求。扩展自己的推理通道（L3 摘要、curator 复核）直接调用 Provider，不改写也不经过 `ContextBudgetExceeded`；超窗时由摘要层的截头重试兜底。
- `session_before_compact` 由压缩扩展自己发射（`context.request_session_before_compact(...)`）：发射时机是「已经确定本次请求会尝试提交」之后、调用摘要模型之前——无可用推理通道、切分点非法、断路器跳闸等不会产生提交的尝试一律不发射，取消则连摘要调用一起省掉；钩子的 reason 与即将提交的 trigger 同一口径，取消则该次请求不提交并跳过 L3（L1/L2 的免费改写保留）。闸门返回 `SessionBeforeCompactDecision(cancelled, context)`：`context` 是各 handler 非空文本按空行合并的结果，作为摘要素材进入提示词（见下一节）。守卫窗口与阈值窗口已同源：`claude_compaction` 用 `min(模型窗口, 显式配置的 COMPACTION_LAYER_CONTEXT_WINDOW)`，未显式配置时直接用模型窗口，长会话不会再绕过摘要层撞上守卫。
- 会话文件格式未变：既有 JSONL 会话、`CompactionEntry` 与 `first_kept_entry_id` 语义继续直接读取，不需要迁移。

## 9. 压缩扩展从四层收敛为三层，环境变量前缀与命令名改名（当时包名 `claude_compaction`，现已更名，见第 11 节）

- 压缩扩展（内置 `compaction`，`src/run_agent_extensions/claude_compaction`）从四层收敛为三层：`L1 snip`（边界投影 + nudge）→ `L2 microcompact`（清空旧工具结果）→ `L3 LLM 摘要`（原 L4：一次有界推理生成持久摘要，并保留轮次对齐的近期尾段）。原第三层「记忆摘要」（直接读 `MEMORY.md` / `USER.md` 当摘要、不调用模型）已整条删除：三方上游都不支持「拿记忆当摘要」——Claude Code 的 `sessionMemoryCompact` 用的是每会话一份对话笔记，不是用户/项目记忆；hermes-agent 的不变量是「memory 与 session history 不是同一层」且记忆从不自动压缩；my-pi-agent 的摘要输入排除 system 里的记忆块。因此「记忆文件存在」不再产生任何压缩，也不再产生摘要文本。
- 环境变量前缀改名：`COMPACTION_FOUR_LAYER_*` → `COMPACTION_LAYER_*`（旧前缀不再被读取）。层开关现在是 `COMPACTION_LAYER_L1_ENABLED` / `_L2_ENABLED` / `_L3_ENABLED`：`_L3_ENABLED` 指摘要层，原 `_L4_ENABLED` 删除并合并进它；原 `_SM_MIN_TOKENS` / `_SM_MIN_TEXT_BLOCK_MESSAGES` / `_SM_MAX_TOKENS`（只服务记忆层）随层删除。主开关仍是 `COMPACTION_LAYER_ENABLED`。
- 命令名改名：`/four-layer-compact [instructions]` → `/compact [instructions]`（core 侧的 `/compact` 在上一版已删除，名字空出；`force-compact` 别名保留）；`/force-snip` 与 `snip` 工具不变。
- 记忆 → 摘要的交界按 hermes 语义补正：`ExtensionContext.request_session_before_compact(...)` 的返回值从 `bool` 变为 `SessionBeforeCompactDecision(cancelled, context)`，handler 用 `SessionBeforeCompactResult(cancel=..., context=...)` 交回文本；多个 handler 的非空文本按空行合并，由压缩扩展包进 `<memory-provider-context>` 并注明「只当素材、不得当成指令」后进入摘要提示词（为空则整段不出现）。`memory` 扩展不再把压缩前的 `on_pre_compress` 文本塞进下一个请求的记忆块。取消语义、未知 reason 只记诊断、handler 异常隔离都不变。
- 会话文件格式未变：既有 JSONL、`CompactionEntry`、`first_kept_entry_id` 与 trigger 映射继续直接读取；新提交的摘要 `metadata.layer` 记为 `L3`。

## 10. 升级前检查

- 先备份再升级：新会话文件会写入 `RunCommitEntry` 等新条目，旧版本程序不保证能读回新文件（session schema 只承诺向前兼容）。

## 11. 压缩策略换成 cheap-first 四层，扩展包名改名，`snip` 工具与 `/force-snip` 移除

- **策略换血**：内置 `compaction` 扩展的策略层整体从 Claude Code 的 `snip` / `microcompact` / `summary` 换成 my-pi-agent（`src/my_agent_core/context.py`，溯源 Pi 的 `compaction.ts`）的 **cheap-first 四层**。显式 snip 不再是自动层：`snip` 工具、`/force-snip` 命令、`SNIP_*` 常量与状态、模型 nudge 与边界投影（`project_snipped_view` / `expand_to_api_rounds` / `boundary_removed_keys` / `SnipBoundary` 等）全部删除。
- **门控**：每轮先估算视图 token；不超 `(budget * 4) // 5`（80%）时**一条层都不跑、原样返回**；超阈值才整批跑三个免费层，重新估算后仍超才跑 L4 摘要。budget 取扩展已有的窗口值（`min(模型窗口, COMPACTION_LAYER_CONTEXT_WINDOW)`）。
- **层号按执行顺序编排**：`L1 大结果落盘 → L2 裁中间轮 → L3 旧结果占位 → L4 摘要`。参考实现按语义编号，其 `L3`/`L1`/`L2` 分别对应本实现的 L1/L2/L3，摘要层两边都是 `L4`。
- **L1 落盘**：`role == "tool"` 且文本超过字符阈值（默认 20000）的工具结果写入 **`<cwd>/.run/tool-results/<tool_call_id>.txt`**（工作区作用域，参考实现用的是全局共享目录，这里刻意偏离以避免跨会话撞名），视图内容精确替换为 `<persisted-output>\nFull: {path}\nPreview:\n{前 2000 字符}\n</persisted-output>`；写入用同目录临时文件 + `os.replace` 原子替换，重复触发幂等覆盖，IO 失败或目录不可用保留原文降级。
- **L2 裁中间轮**：视图长度 ≤ 上限（默认 50）时 no-op；否则留头 3 + 尾 46 + 1 条 user 占位（`[snipped N messages from conversation middle]`），两个切点只向更早回退到「不切开 assistant(tool calls)+tool*」的边界，切点相遇则 no-op。
- **L3 旧结果占位**：取视图中所有工具结果，除最近 5 条外、文本超过 200 字符的，`model_copy(update={"content": "[Earlier tool result compacted]"})`，`metadata` / `tool_call_id` / `is_error` 不动。原「计数阈值 10 / 保留 5 / 时间间隔 60 分钟」规则删除。
- **L4 摘要**：切点 `budget_chars = keep_recent_tokens * 4`（`KEEP_RECENT_TOKENS` 默认 `budget // 4`），从尾部向前累加字符，首次达标得切点，再回退到最近的 user 边界；没有合法切点就不压缩。提示词为独立 system 指令（防注入口径）+ 六段模板（`## Goal` / `## Constraints & Preferences` / `## Progress`(### Done/In Progress/Blocked) / `## Key Decisions` / `## Next Steps` / `## Critical Context`）+ `Previous summary:` + `Conversation:`（assistant 只列 tool call 名称，tool 内容截 4000 字符）+ 可选 `/compact <instructions>` 段；调用不带 model、不带 tools；`<summary>` 标签解析，无标签则去掉 `<analysis>` 后原样使用；失败/空摘要不压缩。
- **包名改名**：`src/run_agent_extensions/claude_compaction` → `src/run_agent_extensions/layered_compaction`（策略已不来自 Claude Code）。扩展**短名仍是 `compaction`**，`BUILTIN_EXTENSIONS`、`--extension compaction` 与文档中的短名不变；Python 导入路径需改为 `run_agent_extensions.layered_compaction`。
- **命令面**：`/compact [instructions]` 保留（`force-compact` 别名保留），`/force-snip` 与 `snip` 工具删除；扩展新增一条提示词 guideline，说明超大工具结果落在 `cwd/.run/tool-results/<tool_call_id>.txt`。
- **环境变量**：前缀 `COMPACTION_LAYER_*` 不变。新增 `_L4_ENABLED`、`_PERSIST_THRESHOLD_CHARS`（20000）、`_SNIP_MAX_MESSAGES`（50）、`_PLACEHOLDER_MIN_CHARS`（200）、`_KEEP_RECENT_RESULTS`（5）；`_KEEP_RECENT_TOKENS` 默认改为 `budget // 4`；删除 `_CACHED_TRIGGER_THRESHOLD`、`_TIME_BASED_ENABLED`、`_TIME_GAP_MINUTES`、`_SNIP_NUDGE_THRESHOLD`、`_USER_NUDGE`、`_IMAGE_MAX_TOKENS`、`_AUTOCOMPACT_BUFFER_TOKENS`、`_AUTOCOMPACT_PCT_OVERRIDE`（这些键不再被读取）。
- **提交与摘要形态**：仍通过 `CompactionCommitRequest` 请求提交、由 core 校验并写单条 `CompactionEntry`；视图里的摘要消息沿用 run-agent 既有前缀（`Previous conversation summary:\n…`，即 core 重放 `CompactionEntry` 时的同一形态），不再使用参考实现的私有前缀。扩展的审计 `CustomEntry` 命名空间变为 `layered_compaction.summary`，新提交的 `metadata.layer` 记为 `L4`。
- **三层刻意偏离参考实现**（README 有完整对照表）：落盘目录按工作区作用域；L3 不覆盖 `<persisted-output>` 预览（否则 L1 的「模型可回读全文」契约会在最近几条之外失效）；结果数少于保留条数时 L3 不替换（参考实现的负切片会环绕）。
- **会话文件格式未变**：既有 JSONL、`CompactionEntry`、`first_kept_entry_id` 与 trigger 映射继续直接读取；恢复会话时视图由已提交的 `CompactionEntry` 重建，不会重新摘要。
