# Run Agent（Harness Coding Agent）

## 项目简介

Run Agent 是一个基于 Python 实现的 **自进化 Harness Agent**。它不是简单的命令行聊天工具，而是一个可运行、可阅读、可扩展的本地 Coding Agent Runtime：统一编排大模型推理、工具调用、文件编辑、Shell 执行、权限控制、长期记忆、Skills、自进化、MCP 外部工具、子 Agent 和会话恢复。

项目重点是 **Harness**：模型只负责推理和提出工具调用意图，真正的环境操作由 Run Agent Runtime 统一做权限判断、工具执行、结果回写、上下文压缩和经验沉淀。它适合学习 Claude Code 类工具的底层机制，也适合作为个人 Coding Agent、项目分析助手或领域 Agent 的二次开发基础。

品牌为 **Run**，运行时目录为 **`.run/`**。可通过 `python -m agents.main` 启动。

## 系统架构

### 架构特点

- **Harness 闭环**：用户输入 → 调模型 → tool call → 权限闸门 → 执行 → tool result 回写 → 继续推理，直到纯文本回答
- **协议无关的能力层**：OpenAI Chat Completions 与 Anthropic Messages 只分叉「调模型 + 账本形状」，权限 / MCP / 子 Agent / compact / 自进化共用同一套电路
- **权限先于执行**：Plan Mode 下业务写文件与 Shell 硬拒绝，不依赖模型自觉
- **长上下文可续聊**：预算截断、过期结果 snip、结构化 fold 记忆替换历史
- **可沉淀**：长期 Memory 记事实与偏好，Skills 记可复用方法，自进化把用户反馈写成 SKILL.md

### 核心领域

系统将 Runtime 划分为以下核心领域：

- **入口域（CLI）**：参数解析、REPL 斜杠命令、one-shot、会话恢复、计划审批
- **循环域（Loop）**：Agent.chat、双协议模型调用、工具调度、token 记账
- **手脚域（Tools）**：读写文件、精确编辑、检索、Shell、权限模式
- **记忆域（Memory / Session / Fold）**：跨进程长期记忆、会话落盘、可折叠会话记忆
- **方法域（Skills / Evolution）**：SKILL.md 发现检索执行、online add/merge/discard、champion 评测
- **扩展域（MCP / Sub-Agent）**：外部 stdio 工具与隔离上下文的子代理

### 模块结构

```text
run-agent/
├── agents/                          # Runtime 包
│   ├── main.py                      # CLI 入口、REPL、参数解析
│   ├── agent.py                     # Agent Loop、双协议、工具调度、压缩
│   ├── api_config.py                # OpenAI / Anthropic 路由
│   ├── tools.py                     # 内置工具与权限闸门
│   ├── prompt.py                    # System prompt 动态构建
│   ├── ui.py                        # 终端展示（不执行工具）
│   ├── session.py                   # 会话保存与恢复
│   ├── session_memory.py            # fold transcript / JSON 解析
│   ├── memory.py                    # 长期记忆写入、索引、召回
│   ├── skills.py                    # Skills 发现、检索、执行、演化包装
│   ├── online_skill_evolution.py    # 在线抽取与 add/merge/discard
│   ├── skill_evolution.py           # 落盘、版本、provenance 审计
│   ├── online_skill_eval.py         # 在线 Skill 评测（replay / champion）
│   ├── mcp_client.py                # stdio JSON-RPC MCP 客户端
│   ├── subagent.py                  # explore / plan / general 配置
│   └── frontmatter.py               # SKILL.md / Memory 头解析
├── .run/                            # 运行时（sessions / skills / plans / skill-evolution）
├── echo_mcp_server.py               # 演示用 stdio MCP Server
├── .mcp.json                        # 项目级 MCP 配置
├── .env.example
├── requirements.txt
└── README.md
```

核心运行链路：

```text
用户输入
  -> agents/main.py
  -> Agent.chat()
  -> 构建 Prompt / 检索 Skills / 预取 Memory / 初始化 MCP
  -> 调用 OpenAI-compatible 或 Anthropic-compatible 模型
  -> 模型返回文本或 tool call
  -> Harness 做权限检查
  -> 执行工具 / Skill / MCP / 子 Agent
  -> tool result 回写模型
  -> 保存 Session
  -> 后台执行 Skill usage tracking 和 online skill evolution
```

## 核心功能

### 1. 完整 Agent Loop

- **ReAct 闭环**：推理、提出 tool call、权限检查、工具执行、结果回写、继续推理
- **双协议**：支持 OpenAI-compatible Chat Completions 与 Anthropic-compatible Messages
- **协议路由**：base URL path 含 `/anthropic` 时走 Anthropic，否则走 OpenAI
- **Token 记账**：输入 / 输出累计，`/cost` 查看窗口占用、费用估计与 fold 次数
- **流式输出**：OpenAI / Anthropic stream；可重试 429/过载；只读工具并行
- **Anthropic thinking**：`--thinking` 开启 extended thinking
- **费用帽**：`--max-cost` 超预算停止

### 2. 工具系统与权限控制

- **内置工具**：`read_file`、`write_file`、`edit_file`、`list_files`、`grep_search`、`run_shell`
- **延迟工具**：`enter_plan_mode` / `exit_plan_mode` 经 `tool_search` 激活
- **settings 权限规则**：`~/.run/settings.json` 与 `.run/settings.json` 的 allow/deny
- **五种权限模式**：`default`、`acceptEdits`、`plan`、`dontAsk`、`bypassPermissions`（`-y`）
- **危险命令确认**：rm / git push / sudo 等需用户确认（`-y` 可跳过）
- **Plan Mode 双保险**：Prompt 约束流程 + Permission 硬拒业务写/Shell；唯一可写 `~/.run/plans/plan-<session>.md`

### 3. 会话保存与恢复

- **自动落盘**：每轮写入 `~/.run-agent/sessions/<id>.json`
- **续聊**：`--resume` 最近一次，`--session <id>` 指定会话
- **REPL 选择**：`/sessions` 列表，`/resume` 交互或按序号 / id 直达
- **Fold 审计**：项目 `.run/sessions/` 保存 folded-memory jsonl
- **权限不覆盖**：resume 不改写本次 CLI 的 `permission_mode`，因此 `--plan --resume` 仍为 plan

### 4. 长期 Memory

- **落盘位置**：`~/.run/projects/<sha256(cwd)[:16]>/memory/`（跟人走，不进仓库）
- **写入约定**：`write_file` + frontmatter（`name` / `description` / `type`）
- **召回**：无 tools 的 side query 与主循环并行预取，settled 后注入最后一条 user
- **本地命令**：`/memory` 列表，不交给模型当任务执行

### 5. Skills 体系

- **发现路径**：用户级 `~/.run/skills/<name>/SKILL.md` 优先，项目级 `.run/skills/` 补充
- **检索执行**：按任务检索 top-k，经 `skill` 工具 inline 注入正文；`context: fork` 时开子会话
- **与 Memory 分工**：Memory 记跨会话事实/偏好，Skills 记可复用工作流
- **本地命令**：`/skills` 列出已发现 skill

### 6. 可折叠会话记忆与上下文压缩

- **三类记忆**：episode（总任务与进度）、working（眼前子目标）、tool（工具经验与 derived_rules）
- **压缩管线**：budget / snip / microcompact 做局部减肥；fold 用结构化 JSON 替换几乎全部历史
- **触发**：`/compact`、`compact_context` 工具，以及输入 token 超过窗口 70% 时自动 compact
- **大结果落盘**：超限 tool 输出写入 `~/.run-agent/tool-results/`，messages 中保留预览与路径

### 7. MCP 外部工具扩展

- **自研 stdio 客户端**：JSON-RPC，不强制 MCP SDK
- **配置合并**：`~/.run/settings.json` → `.run/settings.json` → `.mcp.json`
- **同环接入**：发现的工具以 `mcp__server__tool` 注册进同一 Loop 与权限闸门
- **Plan Mode**：`mcp__*` 默认 deny，避免不可见的外部副作用
- **演示资产**：`echo_mcp_server.py` + 项目根 `.mcp.json`

### 8. 子 Agent 协作

- **类型**：`explore` / `plan` / `general`，以及 `~/.run/agents`、`.run/agents` 自定义
- **Fork-return**：子 Agent 独立上下文跑完，主会话只回收摘要
- **防递归**：子 Agent 工具集去掉 `agent`；explore/plan 仅只读工具
- **约束**：父为 plan 则子为 plan；否则子 `bypassPermissions` 并靠白名单约束
- **Skill fork**：`context: fork` 的 skill 复用同一套隔离执行

### 9. Skill 自进化与评测

- **Pending window**：本轮回答只挂起，下一轮用户反馈到来再 extract，避免把一次性措辞写成 skill
- **决策**：`add` / `merge` / `discard`；identity 碰撞或相似分 ≥ 0.55 强制 merge
- **审计**：`.run/skill-evolution/` 下 jsonl、索引、history 快照
- **开关**：`RUN_AUTO_SKILL_EVOLUTION`（默认开）、`RUN_AUTO_SKILL_TARGET`（默认 project）
- **完整评测**：`/skill-eval` 冻结 replay 池、规则编译、启发式/LLM 变体、champion 锦标赛与 promote/hold

## 技术栈

### 运行时

- **语言**：Python 3.11+
- **模型协议**：OpenAI Chat Completions（`openai`）、Anthropic Messages（`anthropic`）
- **配置**：python-dotenv
- **终端 UI**：Rich
- **MCP**：自研 stdio JSON-RPC（无官方 SDK 依赖）
- **构建**：pip + `requirements.txt`，本地 `.venv`

## 技术亮点

### 1. Harness 与模型职责分离

模型只出意图（文本或 tool call），Harness 统一做权限、执行、回写、压缩和经验沉淀。MCP 与子 Agent 必须进入同一 Loop，否则确认、session、compact 会被旁路。

### 2. OpenAI / Anthropic 双协议、一套权限

内部工具定义保持统一 ToolDef 形（`name` + `description` + `input_schema`），再分别包装为 OpenAI `function` 与 Anthropic `tools`。OpenAI 使用 `role=tool`；Anthropic 使用独立 `system` + `user/tool_result` 对齐 `tool_use_id`。禁止为第二条协议复制权限实现。

### 3. Plan Mode 双保险

Prompt 规定「只读探索 → 写计划文件 → `exit_plan_mode` 提审」；Permission 在 plan 下拒绝业务写文件与 Shell。审批通过后把 Approved Plan 作为 tool result 塞回 messages，同一 `Agent.chat` 继续执行，而不是另起调度器。

### 4. 结构化折叠记忆，而不是截断最近 N 轮

fold 产出 episode / working / tool 三类 JSON，替换几乎全部原始历史；坏 JSON 走 fallback。配合 budget / snip 管线，长会话可以腾窗口并续上任务。

### 5. 自进化等待下一轮用户反馈

助手本轮输出只是候选行为；用户下一句的肯定、纠正或「以后都…」才是可复用证据。pending window 后再 add/merge，并用 provenance 追责。plan 模式与子 Agent 不跑 online evolution。

### 6. 无锁化思想在库存场景之外的对应：权限闸门 + CAS 式确认缓存

危险操作按 message 缓存确认结果，避免同一路径反复询问；Plan / dontAsk / yolo 用模式切换代替到处打散的 if。高风险写盘路径始终经过 `check_permission`。

## 环境要求

- **Python**：3.11+
- **操作系统**：Windows / macOS / Linux
- **模型接口**：OpenAI-compatible 或 Anthropic-compatible（含常见网关）
- **可选**：ripgrep（增强检索体验）、已配置的 MCP Server

## 快速开始

### 1. 环境准备

```powershell
cd E:\pythonProject\run-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

### 2. 配置修改

编辑 `.env`：

```env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://your-host/v1
MODEL=deepseek-v4-flash
# 可选：REASONING_EFFORT=max
```

Anthropic 兼容时设置 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`，或让 base URL 的 path 包含 `/anthropic`。

### 3. 启动服务

```powershell
# 一键演示
python -m agents.main "用工具摸清本仓库结构并总结"
python -m agents.main "读取 requirements.txt 前两行"

# 进入 REPL（无位置参数）
python -m agents.main

# 权限与续聊
python -m agents.main --plan -y "只读规划仓库结构，不要改业务文件"
python -m agents.main --resume
```

### 4. Anthropic 兼容接口

```powershell
$env:ANTHROPIC_API_KEY = "sk-..."
$env:ANTHROPIC_BASE_URL = "https://your-host/anthropic"
python -m agents.main -y "列出 agents/ 下有哪些 py 文件"
```

### 5. Skill 评测

```powershell
python -m agents.online_skill_eval
```

报告写入 `.run/skill-evolution/online-eval/last_report.json`。

## 项目结构说明

### 入口层（main.py / ui.py）

CLI 与 REPL。斜杠命令必须本地处理，不能当作普通句子发给模型。`ui.py` 只负责终端展示。

### Harness 层（agent.py / prompt.py / api_config.py）

Agent Runtime：协议分叉、工具调度、Memory 注入、compact、自进化后台任务。

### 能力层（tools.py / memory.py / skills.py / mcp_client.py / subagent.py）

手脚、长期记忆、方法库、外部工具、子代理配置。

### 沉淀层（online_skill_evolution.py / skill_evolution.py / online_skill_eval.py）

从对话抽取 skill、落盘升版本、审计与 champion 评测。

## 运行说明

### CLI 参数

| 参数 | 含义 |
|------|------|
| `--yolo`, `-y` | 跳过全部确认 |
| `--plan` | 只读规划模式 |
| `--accept-edits` | 自动放行写文件 |
| `--dont-ask` | 该确认的直接拒绝 |
| `--model`, `-m` | 模型名 |
| `--api-base` | API Base（`/v1` 或含 `/anthropic`） |
| `--thinking` | Anthropic extended thinking |
| `--resume` | 恢复最近会话 |
| `--session ID` | 恢复指定会话 |
| `--max-cost USD` | 费用上限 |
| `--max-turns N` | 工具循环上限 |

### REPL 斜杠命令

`/help` `/exit` `/clear` `/cost` `/sessions` `/resume` `/plan` `/memory` `/skills` `/compact` `/mcp` `/extract_now` `/skill-feedback` `/skill-evolve` `/skill-create` `/skill-stats` `/skill-eval` `/<skill>`

### 运行时目录

| 路径 | 用途 |
|------|------|
| `~/.run-agent/sessions/` | 会话 JSON |
| `.run/sessions/` | fold 审计 |
| `~/.run/plans/` | Plan Mode 计划文件 |
| `.run/skills/` | 项目级 Skills |
| `.run/skill-evolution/` | 自进化审计与评测产物 |
| `~/.run/projects/<hash>/memory/` | 项目级长期记忆 |
| `~/.run/skills/` | 用户级 Skills |
| `~/.run-agent/tool-results/` | 过大的工具输出 |
| `.run/rules/*.md` | 注入 system prompt 的项目规则 |

## 监控与运维

### 会话与成本

- 每轮自动保存 session，可用 `/sessions`、`/resume` 回溯
- `/cost` 查看 token 与 fold 次数

### 审计

- Skill 调用、create/evolve、online ingest 写入 `.run/skill-evolution/*.jsonl`
- 评测报告：`.run/skill-evolution/online-eval/last_report.json`

### 开关

- `RUN_AUTO_SKILL_EVOLUTION=0` 关闭自进化
- `RUN_AUTO_SKILL_TARGET=project|user` 控制落盘目标
- 后台自动写 skill 仅在 `bypassPermissions` / `acceptEdits`（`-y`）下生效，否则可用 `/extract_now` 交互确认

### 当前范围

- 品牌为 **Run Agent**，运行时目录 `.run/`，环境变量 `RUN_*`
- System prompt 会加载 `CLAUDE.md`（向上查找）与 `.run/rules/*.md`
- 无独立 wiki 站点 / Docker 编排；冷启动以本 README 为准
