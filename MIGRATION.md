# 迁移说明

本文件说明旧版本（gateway 与后台 review 控制环的时代）升级到当前 Harness 版本时会遇到的兼容行为。它只描述当前代码里真实成立的行为；升级没有自动数据迁移或清理步骤。

## 1. 会话、记忆与正式 Skill：直接读取

- 旧 Session JSONL（`<cwd>/.run/sessions/<id>.jsonl` 与同目录 `index.jsonl`）直接读取：消息与资源条目仍按 `id/parent_id` DAG 解析，`--sessions`、`--session <id>`、rewind/fork/branch 沿用原文件，不做格式转换。
- 旧 `USER.md`、`MEMORY.md` 直接读取，并按现有字符预算注入提示；`/memory` 与 `memory` 工具继续在原子写、预算和威胁检查下编辑这两个文件。记忆职责已从 `experience` 扩展迁到独立的内置扩展 `memory`（`src/run_agent_extensions/hermes_memory`）：文件路径与格式未变——`~/.run/{MEMORY,USER}.md`（用户作用域）与 `<cwd>/.run/{MEMORY,USER}.md`（项目作用域）继续直接读取，`§` 分隔的条目格式、字符预算、快照冻结和威胁屏蔽语义保持一致。
- 记忆相关环境变量改名：旧 `EXPERIENCE_MEMORY_*` / `EXPERIENCE_USER_*` 不再被读取，等价配置现为 `HERMES_MEMORY_*`（如 `HERMES_MEMORY_CHAR_LIMIT`、`HERMES_MEMORY_USER_CHAR_LIMIT`、`HERMES_MEMORY_ENABLED`、`HERMES_MEMORY_USER_PROFILE_ENABLED`、`HERMES_MEMORY_WRITE_APPROVAL`）。`EXPERIENCE_*` 仍保留 Skill 演进相关项（`EXPERIENCE_SKILLS_WRITE_APPROVAL`、`EXPERIENCE_SKILL_GUARD`、`EXPERIENCE_SKILL_LEDGER`、`EXPERIENCE_EVOLUTION_*`）。
- 正式 Skill（`<skills-root>/<name>/SKILL.md`）由标准 loader 直接发现并冻结进会话资源快照，不要求旧 frontmatter 具备新字段。

## 2. 旧 sidecar 与旧 ledger：保留、可查、可 rollback

- `.usage.json`、`.archive/`、`.ledger.jsonl` 及 ledger 的 `.blobs/` 不会被删除或改写（`src/run_agent_extensions/experience/skill_usage.py`）。旧 `.usage.json` 现在只读：`pinned` 仍是发布/采纳的 policy 输入（`skill_usage.py:55-56`、`skill_manager.py:189-190`），consultation 计数不再更新。
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

- `run gateway`、`/review`、`/curator`，以及 `FEISHU_*`、`GATEWAY_*`、`EXPERIENCE_REVIEW_*`、`EXPERIENCE_CURATOR_*` 和旧 nudge 配置都不再被读取或提供。当前 Skill 演进命令面是 `/evolve status|candidates|show|adopt|publish|reject|ledger|rollback`。

## 8. 升级前检查

- 先备份再升级：新会话文件会写入 `RunCommitEntry` 等新条目，旧版本程序不保证能读回新文件（session schema 只承诺向前兼容）。
