# 秋招简历项目表述

## 推荐项目名称

```text
Run Agent：基于 Python Extension Host 的可验证 Coding Agent
```

以下内容只写当前源码可直接定位的能力，不填写尚无完整 evidence pack 的评测数字。

## 可直接替换的 HTML

```html
<div class="proj-head">
    <div class="text-base font-bold proj-name">Run Agent：基于 Python Extension Host 的可验证 Coding Agent</div>
    <a class="proj-link" href="https://github.com/zhanglinrun/run-agent">github.com/zhanglinrun/run-agent</a>
    <div class="text-sm font-bold proj-date">2026.05 - 至今</div>
</div>
<div class="text-sm proj-desc">
    <div class="tech-line"><span class="font-bold">技术栈：</span>Python、OpenAI SDK、Anthropic SDK、MCP、SQLite、Docker、SWE-bench</div>
    <span class="font-bold">项目描述：</span>参考 Pi 的组合式架构，从零实现 provider-neutral AgentCore 与版本化 Python Extension Host；CLI 默认加载完整能力 profile，并以统一 TaskResult 和 append-only SQLite Session 保存执行证据。<br>
    <span class="font-bold">核心职责：</span>
    <ul>
        <li><span class="font-bold">Extension-first 架构：</span>将 AgentCore 限定为 Provider、Turn、ToolExecutor 与 Hook 协议；通过 setup(api) 注册工具、事件、命令、Prompt、服务和执行环境，支持依赖排序、原子回滚、自动发现及按能力关闭。</li>
        <li><span class="font-bold">权限与上下文边界：</span>统一授权模型工具、MCP 启动与子 Agent；Plan Mode 无审批时 fail closed 并移除 Shell；子 Agent 工具集强制取父权限、角色和 Skill allowed-tools 的交集；每 Turn 从不可变 base prompt 重建动态上下文。</li>
        <li><span class="font-bold">结构化记忆与会话：</span>将长历史折叠为 episode、working、tool 三类 memory 并保存到 SQLite 事件树；通过独立 side query 做语义 Memory 召回，召回内容仅作用于当前请求，持久化写入走专用 memory_save 工具。</li>
        <li><span class="font-bold">环境验证与有界纠错：</span>模型 Solve 后由独立 Verification Extension 执行 patch/syntax/diff/focused checks；失败证据进入预留 Repair Turn；候选恢复限定 disposable 临时 Workspace，并校验 artifact 路径、SHA、base commit 与 git apply。</li>
        <li><span class="font-bold">评测与受控进化：</span>提供 Coding Task、SWE-bench Verified、GAIA/HLE 入口与 manifest/trace；在线 Skill 反馈先进入 Candidate，只有 pending 状态且 replay、boundary、retention 证据通过后才通过统一写路径更新 active Skill。</li>
    </ul>
</div>
```

版面只能容纳四条时，保留前四条，把评测与 Skill Candidate 压缩进项目描述。

## Claim-to-source 证据

面试前按下表复核。详细调用链见
[`docs/architecture.md`](./docs/architecture.md)。

| 简历 claim | 直接源码证据 |
| --- | --- |
| provider-neutral AgentCore | `agents/runtime/core.py::AgentCore` 及其 import 边界 |
| `setup(api)` 第三方扩展 API | `agents/extensions/contracts.py::ExtensionAPI`；`agents/extensions/loader.py::load_extension_spec` |
| 依赖排序、缺失/循环检测 | `agents/extensions/host.py::ExtensionHost._sort_specs` |
| 扩展 setup 原子回滚 | `agents/extensions/host.py::_snapshot`, `_restore`, `load` |
| 默认完整 profile / 独立禁用 | `agents/extensions/defaults.py::_DEFAULTS`, `default_extension_specs`；`agents/cli.py` extension 参数 |
| Project extension 显式信任 | `agents/harness/task.py::ExtensionSettings.trust_project`；`agents/extensions/loader.py::discover_extension_specs` |
| 每 Turn 重建 prompt | `agents/extensions/host.py::transform_context` |
| 结构化 episode/working/tool fold | `agents/context/folding.py`；`agents/extensions/context.py::setup_context` |
| 语义 recall 且不污染 session messages | `agents/context/memory.py::select_relevant_memories`；`agents/extensions/context.py::setup_memory.recall` |
| Plan 无审批 fail closed | `agents/extensions/policy.py::setup_plan.exit_plan` |
| Plan 禁 Shell 且子 Agent 继承 | `setup_plan.enter/session_start`；`agents/extensions/subagents.py::SubagentService.run` |
| 父/角色/Skill 工具交集 | `SubagentService.run` 中 `child_names` 计算；`ExtensionToolExecutor.allowed_names` |
| Skill malformed allowlist fail closed | `agents/evolution/skills.py::_load_skill_file` |
| MCP 启动前授权 | `agents/extensions/policy.py::setup_mcp.session_start` |
| 每 Turn token/cost 扣账 | `agents/runtime/hooks.py::TurnResult.usage`；`agents/harness/middleware.py::BudgetMiddleware` |
| Verification 先于 Correction | `agents/extensions/defaults.py` 依赖；`agents/extensions/quality.py` handlers |
| 候选恢复 hash/base/apply 检查 | `agents/extensions/quality.py::restore_candidate` |
| Acceptance 失败影响 TaskResult | `agents/extensions/quality.py::setup_acceptance`；`agents/harness/harness.py::_status` |
| Candidate 状态机 | `agents/evolution/candidates.py::promote_candidate` |
| typed RuntimeConfig 替代 metadata locator | `agents/harness/task.py::RuntimeConfig`；evaluation 的 TaskSpec 构造 |

## 90 秒面试讲法

> 这个项目的重点不是再封装一次模型 API，而是把 Agent runtime 与可选能力彻底分开。AgentCore 只知道 Provider、消息、Tool Call 和 Hook；权限、Plan、MCP、Context、Memory、Skills、子 Agent、Verification 和 Correction 都是同一个 Python Extension Host 上的插件。第三方模块也使用版本化 setup(api)，可以注册工具、事件、命令和 Prompt，并由依赖图确定加载顺序。
>
> 我把权限边界集中到一个 authorizer。Project extension 默认不执行；本地 Shell 默认不暴露，因为 cwd 不是 sandbox；Plan Mode 没有明确审批不能退出，而且进入后会从父工具 ceiling 移除 Shell。子 Agent 再取父 active tools、角色工具和 Skill allowed-tools 的交集，所以 Coder 或 fork Skill 不能抬高父权限。
>
> 上下文每 Turn 从 immutable base prompt 重建，避免 Memory/Skills 反复追加。长 Session 用 episode、working、tool 三类结构化 Fold；长期 Memory 先做 side-query 语义选择，只把命中的完整内容放进当前请求，不写回消息历史。
>
> 对 Coding 结果，模型说完成后还要经过独立 Verification。失败会使用单独 Repair 预算，候选 rollback 只允许在 disposable 临时 Workspace，并在 reset 前检查 patch 路径、SHA、base commit 和 apply。最终统一输出 TaskResult，评测层只消费这个契约。

## 高频追问边界

### 和 Claude Code / Pi 有什么区别？

> Pi 是架构参考。我在 Python 中实现了小 Core 加组合式 Extension Host，并把验证、纠错、Candidate-first Skill evolution 和评测证据作为研究重点。不声称功能或效果超过成熟商业产品。

### 为什么默认不开放本地 Shell？

> subprocess 的 cwd 不是文件系统 sandbox，命令前缀分类也不能防止绝对路径或输出参数。因此 host-local Shell 只在显式 `allow_host_shell` 后注册；默认命令执行路径是 Docker。Reviewer/Verifier 不拿 Shell，Plan Mode 也会移除 Shell。

### Verifier 会不会仍被模型骗？

> Completion gate 使用退出码、workspace 状态和 patch apply 结果，不使用生成模型的自评。正式 SWE-bench pass/fail 仍以官方 grader 为准。当前没有完成正式 campaign，因此简历不写解决率提升数字。

### 自进化是不是模型改自己源码？

> 不是。进化对象是 Skill。反馈只创建 pending Candidate；状态机和 replay/boundary/retention 证据通过后，才通过既有 create/evolve 写路径更新 active Skill。

## 量化结果规则

只有仓库存在完整 evidence pack 时才加入数字：

- 固定数据清单、SHA、模型、Provider、temperature、seed、权限、工具和预算；
- baseline/candidate 相同 case id；
- 每题 prediction、patch、trace、失败原因及 manifest；
- SWE-bench 使用官方 report；
- 配对统计输出可复核。

当前不要声称：

- GAIA/HLE 的历史百分比提升；
- 已完成 50/500 题 SWE-bench 正式成绩；
- host-local Shell 具有 workspace sandbox 保证；
- 一次反馈可自动覆盖 active Skill；
- 已达到或超过 Claude Code/Pi 的效果。

## 验证状态措辞

本次 `0.4` 重构完成了源码和调用链静态核查，但没有执行项目测试、构建或
live provider/sandbox/MCP 验证。因此在获得对应运行证据前，只能表述“已实现”
和“源码可定位”，不能表述“所有测试通过”或“生产验证完成”。
