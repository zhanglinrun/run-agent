# Run Agent 改造执行记录

执行依据：`study/简历五条/00-RunAgent完整改进执行计划.md`（2026-09-09 修订 3）。用户已授权完整实施以及清理本项目旧开发数据，不提供旧版兼容。

完整范围保存在 `requirements.json`：64 项实施任务、41 项行为验收。只有实现和对应验证都有证据时才标记完成；阶段进展不代表整个目标完成。

- 分支：`codex/harness-redesign`。
- 原始代码、参考仓库版本和用户已有变动：`baseline-state.json`。已有的 `tests/` 删除保留，新的验收放在 `tests/redesign/`。
- 原始调度与存储性能：`baseline-runtime.json`、`baseline-storage.json`。这些是模拟负载，用于同条件比较，不代表模型能力。
- 新版目标：Provider / Core / Coding / Gateway；唯一产品命令 `run`；SQLite 权威状态；独立 Gateway；Session 扩展装配经验学习；独立环境验收和证据闭环。

当前检查点：10 项实施任务完成，A08 与 S04 的新版应用行为验收通过。其他完整目标继续开放；详见 `checkpoint-03.md`、`telemetry-tests.xml` 及 `requirements.json`。早期检查点保留为历史记录。

已落地并验证：

- 唯一发行启动器 `run`，wheel/sdist 包含入口；干净安装、源码目录外的命令路由和 SQLite 创建/重开通过。
- SQLite 独立工作线程、有界请求、短事务、会话分支、分页、水位、幂等追加、单写者资格与世代校验。
- 扩展命名空间状态 CAS、不可变资源、资源头原子发布、内容寻址产物、一致性备份和新目录恢复。
- 删除 Mem0 集成；移除包初始化中的隐式界面装配，修复因此暴露的模型目录和 UI 循环导入。
- 34 项新版组件测试通过；全量 mypy 检查 153 个源文件通过。原有用户删除的测试未恢复。

验证命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/redesign -q
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m ruff check src extensions scripts tests/redesign
```

旧调度基线的完整原始证据在 `baseline-runtime.zip`；解压后可用基线提交中的 `rebuild_runtime_benchmark` 复核。压缩包中的逐行文件仅为原版本的历史实验产物，新产品不以它们作为运行或恢复入口。

`storage-component-results.json` 是新 repository 的单机模拟测量，尚未覆盖整个 CodingSession 路径，不直接用它填写简历中的端到端性能数字。

检查点 02 已接通 CodingSession、application 和 Gateway 会话池的 SQLite 生命周期与最终提交回执，统一 Rich / prompt_toolkit 终端，删除会话 JSONL、RPC、Textual 呈现和组件 API。50 项新版测试通过；全量 mypy 检查 142 个源文件通过。全新 wheel 安装可从源码外完成模拟任务并恢复会话。

检查点 03 已移除诊断、观测和评测的剩余 JSONL，正式评测使用 SQLite application，失败/取消保留可得费用。63 项新版测试、144 个源文件 mypy、Ruff、wheel/sdist 和全新安装通过。

下一阶段补齐 HostServices、受管理扩展任务与资源快照，继续持久网关、experience、独立验收、真实模型样本与消融。Outbox 尚未实现，模拟模型测试不代表真实编码能力。
