# 清理记录 01：旧开发状态（P0-8）

日期：2026-09-10。依据计划 §12.2 执行；用户在原问卷中勾选 P0-8，并在执行前对仓库外路径
单独确认「全部删除」。本轮只清理开发状态，不删除源码、参考仓库、测试或配置模板。

## 1. 仓库内 `.run/`（已授权范围）

先打印绝对路径、大小与修改时间，再删除。共 10 个目标，释放 **734.1 MB**：

| 目标 | 大小 | 性质 |
| --- | --- | --- |
| `.run/redesign` | 729.7 MB | 前序 checkpoint 的 25 组 `dist-*` 与 `install-env-*`、旧基线目录、旧脚手架脚本（含 `migrate_session.py` 迁移脚本） |
| `.run/benchmarks` | 2.6 MB | 旧基准产物 |
| `.run/final-dist` | 1.2 MB | 旧发行产物 |
| `.run/preflight-wheel` | 0.6 MB | 旧预检 wheel |
| `.run/evals` | 0 MB | 空目录 |
| `.run/section9.txt` 等 4 个 | 0 MB | 阅读计划文档产生的临时切片 |
| `.run/update_requirements.py` | 0 MB | 旧的账本更新脚本，已被 `scripts/requirements.py` 取代 |

删除后 `.run/` 只剩两项，均为刻意保留：`.run/skills`（项目 Skills，`.gitignore` 的白名单例外）
与 `.run/verify`（当前闸门的构建/安装缓存）。

删除前的完整清单已写入 `.run/verify/p0-8-inventory.txt`（该路径在 `.gitignore` 内，仅作本机记录）。

## 2. 仓库外状态目录（执行前单独确认）

项目真实状态目录由 `RunAgentPaths`（`src/run_agent_coding/paths.py:20`）解析为
`C:\Users\zlr\.run`，**在仓库根目录之外**，因此先单独取得用户确认再删除。

清点内容全部为旧格式遗留物：

| 目标 | 大小 | 性质 |
| --- | --- | --- |
| `sessions/pythonproject-run-agent-eb7db4/` | 120.3 KB | 6 个产品会话 JSONL + `index.jsonl` + `.lock`，含 `refactor-smoke-*` |
| `sessions/gateway/` | 0 KB | 4 个 `gateway-*.jsonl` 与锁，即已删除的 stdin_jsonl 渠道路径 |
| `tool-results/` | 422.9 KB | 9 个旧 `read_file` 产物 |
| `traces/` | 2.9 KB | 4 个 trace JSONL |
| `logs/` | 8.5 KB | 旧日志 |
| `projects/` | 0.3 KB | 约 380 个 Mem0 时代的空 `memory/` 目录 |
| `state/`、`plans/`、`cache/` | 0 KB | 旧扩展状态、计划、缓存 |
| `trust.json`、`trust.json.lock`、`tui.json` | 0.7 KB | 旧配置；`tui.json` 属于已删除的 Textual 界面 |

删除方式：保留 `C:\Users\zlr\.run` 目录本身，删除其全部内容；每条路径都先校验
`StartsWith(stateHome + separator)`，否则直接抛错拒绝执行。

**结果**：`[System.IO.Directory]::GetFileSystemEntries` 返回 0 条，递归枚举 0 条，目录已空。

**诚实记录一处不一致**：清点阶段列出 13 个条目，删除阶段枚举到 11 个并报告删除 11 条。
未出现在删除阶段列表中的两个文件（`agent-calls.jsonl`、`release-notes-state.json`）经直接
`Test-Path` 与原始目录枚举确认为**不存在**。因此最终状态（目录为空）是确定的，但
「11 还是 13」这一计数差异我没有查清原因，不声称自己删除了 13 条。

该目录内**不存在** `state.sqlite3`，即其中没有任何新版数据，删除不会丢失任何当前格式的状态。

## 3. 保留项

- `.run/skills`、`.run/verify`（仓库内）。
- `C:\Users\zlr\.agents\skills` 下的 `agent-browser-cli`、`find-skills`（属于 Pi 的 agent 技能，
  与本项目状态无关）。
- 源码、`tests/`、`docs/`、`study/`、`extensions/`、`.env`、参考仓库：一个未动。
  删除后复核：`src` 149 个 `.py`、`tests` 27 个 `.py`、`docs/implementation` 55 个文件。

## 4. 冷启动验收

用全新状态目录（`RunAgentPaths(home=<fresh>)`）验证空状态初始化与重启持久化：

```text
fresh home          : E:\pythonProject\run-agent\.run\verify\coldstart
db exists before    : False
opened and created  : True
user_version/app_id : (8, 1381322305)
reopened after close: True
tables created      : 21
sample tables       : artifact_refs, artifacts, branches, context_snapshots, entries,
                      execution_revocations, executions, extension_owners
COLD START OK
```

完整生命周期（建会话、追加、分支、恢复）由既有测试覆盖并随闸门通过：
`tests/redesign/test_sqlite_sessions.py::test_cold_start_persistence_reopen_and_fork`、
`tests/redesign/test_coding_application.py::test_application_commits_receipt_reopens_and_branches`。

## 5. 产品 JSONL 审计

全仓库（排除 `.venv/` 与 `.run/`）仅剩一个 `.jsonl`：

`evals/coding/smoke/tasks.jsonl` —— 这是**评测任务清单输入**，不是产品会话状态。
计划 §7.1.2 删除的是会话存储格式，并明确保留 JSON 用于任务、配置、报告与单次机器输出；
§7.1.2 还明确「用户工作区可能包含任意 .jsonl 数据文件，通用文件工具仍应能读取」。
S08 的「无产品 JSONL 文件」断言应据此区分：允许评测任务清单，禁止会话/轨迹/账本/逐行 RPC
的 JSONL 读写路径。
