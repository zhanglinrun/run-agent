# 经验学习：对齐 Hermes 的设计

用户指定参考 `E:\pythonProject\hermes-agent`。以下是从其源码提取的设计要点，以及**它暴露出的本项目缺口**。
只记录从代码读到的事实，不转述计划文档的概括。

## 1. 记忆（USER.md / MEMORY.md）

| 要点 | Hermes 的做法 | 出处 |
| --- | --- | --- |
| 条目分隔 | `ENTRY_DELIMITER = "\n§\n"`（§，条目可多行） | `tools/memory_tool.py:78` |
| 预算单位 | **字符而非 token**，理由写死在注释里：「Character limits (not tokens) because char counts are model-independent」 | `memory_tool.py:17` |
| 默认额度 | `memory_char_limit = 2200`、`user_char_limit = 1375` | `memory_tool.py:178-179` |
| 超限行为 | **报错并回传当前用量**：`Memory at {current:,}/{limit:,} chars. Adding this entry ... would exceed the limit.`，附 `"usage": "current/limit"` | `memory_tool.py:442-464` |
| 写入闸门 | `_apply_write_gate` / `_apply_batch_write_gate` / `apply_memory_pending`，写入先待批 | `memory_tool.py` |
| 防止耗尽 | `_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3`，避免合并重试吃光预算、压掉用户回复 | `memory_tool.py:174` |

**对齐结论**：预算用**字符**（模型无关），超限要**回传用量**而不是静默截断，写入要有闸门。
我当前的 experience 扩展用的是 `max_tokens` 做资源选择——这是要改的。

## 2. 后台复盘

| 要点 | Hermes 的做法 | 出处 |
| --- | --- | --- |
| **与前台互斥** | `cancel_background_review_for_live_turn`：一旦有实时回合开始就取消复盘 | `agent/background_review.py` |
| 输入预算 | `_review_input_token_budget(task_cfg)` 读 `auxiliary.background_review.max_input_tokens`；`<= 0` 表示显式不设上限 | 同上 |
| 配置读取 | `load_background_review_settings()` 单次读取；**出错时 fail-open（enabled=True）并打 WARNING**，理由是「不能让坏配置静默关掉这条消耗成本的路径」 | 同上 |
| 成本归因 | `_record_review_usage_to_parent` 把复盘用量记回父运行；`_snapshot_review_usage` | 同上 |
| 上下文 | 快照最近消息（`DEFAULT_CONTEXT_MESSAGES = 10`，单条上限 `_MESSAGE_CHAR_CAP = 12_000`）**加上本次实际加载过的 Skill**（`collect_parent_loaded_skills`） | `agent/review_engine.py` |
| 结果分类 | `_classify_review_result(actions)`、`summarize_background_review_actions` | `background_review.py` |

**对齐结论**：我在 P5-1 里只做了「冷却」。Hermes 的做法更对——**复盘与前台互斥**（前台一忙就取消），
而且**复盘自己的用量要记回父运行**，否则"学习成本"是不可见的，简历里"额外成本"就报不准。

## 3. Skill 的进化与生命周期（最关键的一条）

**写来源（provenance）决定归属**，`tools/skill_provenance.py` 全文 78 行，用 ContextVar 表达：

> The curator only consolidates/prunes skills it autonomously created via the background
> self-improvement review fork. **Skills a user asks a foreground agent to write belong to the
> user and must never be auto-curated.**

取值：`"foreground"`（默认，CLI/网关/cron/子代理的普通工具调用）与 `"background_review"`（自我改进复盘分支）。
只有后者创建的 Skill 才标记为 `agent-created`，可被 curator 管理。

生命周期常量（`agent/curator.py`）：

```text
DEFAULT_INTERVAL_HOURS = 24 * 7      # 7 天
DEFAULT_MIN_IDLE_HOURS = 2           # 至少空闲 2 小时才跑
DEFAULT_STALE_AFTER_DAYS = 30        # 30 天未用 → stale
DEFAULT_ARCHIVE_AFTER_DAYS = 90      # 90 天 → archive
DEFAULT_CONSOLIDATE = False           # 默认关闭合并
```

配套守卫与校验（`tools/skill_manager_tool.py`）：

- `_pinned_guard`：用户固定/pinned 的 Skill 不自动删
- `_security_scan_skill`、`_guard_agent_created_enabled`、`_curator_consolidation_delete_guard`
- `_background_review_write_guard`、`_background_review_read_before_write_guard`：复盘写之前必须先读过
- `_validate_delete_target`、`_is_path_redirect`、`_containing_skills_root`：防符号链接/路径逃逸
- 限额：`MAX_NAME_LENGTH=64`、`MAX_DESCRIPTION_LENGTH=1024`、`MAX_SKILL_CONTENT_CHARS=100_000`、
  `MAX_SKILL_FILE_BYTES=1_048_576`、`VALID_NAME_RE=^[a-z0-9][a-z0-9._-]*$`、
  `ALLOWED_SUBDIRS={"references","templates","scripts","assets"}`
- `apply_automatic_transitions()`：stale/archive 是**按时间的自动状态转移**，与删除是两回事
- 支持 dry-run（`CURATOR_DRY_RUN_BANNER`）与运行报告落盘

**对齐结论**：这是本项目**最实质的缺口**。我的 P4 已有作用域（project/user）与不可变版本，
但**没有写来源（provenance）**，因此无法区分「用户让我写的 Skill」与「复盘自己生成的 Skill」——
而这条区分正是"自动整理不会误删用户资产"的唯一依据。

## 4. 由这次对齐产生的计划变更

| 影响 | 内容 |
| --- | --- |
| **P4 需要重新打开** | 已标 complete，但缺：①USER/MEMORY 用**字符**预算并在超限时回传用量；②**写来源 provenance**（用户写 vs 复盘写）。这不是可选优化，而是第三条简历「可插拔的经验学习」的成立前提 |
| **P5-1** | 「冷却」保留，但增加 **与前台互斥**（前台回合开始即取消复盘） |
| **P5-2** | 复盘预算按 Hermes 口径：`max_input_tokens` 显式可关（`<=0`），配置读取 **fail-open + WARNING**，上下文 = 最近消息快照 + 本次真实加载的 Skill |
| **P5-3/P5-5** | 复盘用量**记回父运行**，使额外成本可见；发布规则要能区分来源 |
| **P4/P5 维护** | 引入 stale/archive **按时间自动转移**（30/90 天）、pinned 保护、限额校验、dry-run 与运行报告 |

**这是我主动报告的扩大范围**：承认 P4 关早了，并把 Hermes 的 provenance 视为必做项，
而不是"看起来差不多就算完"。
