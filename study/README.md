# Run Agent 手敲学习计划

## 结论：按 runcli 同一套演进方式手敲，收口为完整可演示产品

对照你已经做过的 `E:\javaproject\runcli`：初始化就可运行 → 加 Plan / Memory / Skill / MCP … → 完整项目。

本仓库同样：

1. **C01** = 可运行的最小 ReAct Coding Agent
2. **C02–C10** 每次加一块完整能力
3. **终点** = 完整 Harness 能力（AgentCore + AgentHarness + Extensions）

教程形式对齐：`AI-Engineer-from-scrach/08-hermes-agent/01-memory/notebooks`
→ 用 **Jupyter Notebook** 边讲边跑，再把代码落入 `agents/`。

## 对照与落点

| 角色 | 路径 |
|------|------|
| 演进范本 | `E:\javaproject\runcli` |
| 功能终点 | 完整 Harness（本仓库） |
| 手敲落点 | `run-agent/agents/` |
| **主教程** | [`study/notebooks/`](./notebooks/) |

## 文档导航

| 文件 | 用途 |
|------|------|
| [docs/architecture.md](./docs/architecture.md) | Extension-first 架构与 claim-to-source 证据 |
| [docs/extensions.md](./docs/extensions.md) | 第三方 Python `setup(api)` 扩展 API 与信任模型 |
| [docs/sandbox.md](./docs/sandbox.md) | Docker Sandbox 安全边界、生命周期与本地/容器切换 |
| [docs/swebench.md](./docs/swebench.md) | SWE-bench Verified 数据钉死、campaign 与 harness 消融 |
| [architecture.md](./architecture.md) | Harness 分层、五平面、执行闭环与设计目标（学习向总览） |
| [eval-benchmarks.md](./eval-benchmarks.md) | Smoke / Coding / SWE-bench / GAIA-HLE、Trace 证据与消融设计 |
| [online-skill-eval.md](./online-skill-eval.md) | Candidate-first Skills：replay、门禁、LLM judge、active promotion |
| [resume-project.md](./resume-project.md) | 可直接替换的简历 HTML、量化发布标准与面试问答 |
| [ARCHITECTURE_DIAGRAMS.md](./ARCHITECTURE_DIAGRAMS.md) | 全项目架构图、运行流程图与时序图 |
| [run-agent-interview-notes.md](./run-agent-interview-notes.md) | 秋招面试展开笔记 |
| [notebooks/](./notebooks/) | **C01–C10 手敲教程（ipynb）**；历史演进路径，不随本次 docs 迁移改写 |
| [PROGRESS.md](./PROGRESS.md) | 打卡 |

## 怎么用

1. 先读 [`docs/architecture.md`](./docs/architecture.md) 建立当前仓库边界感
2. 打开 `study/notebooks/C01_react_agent.ipynb`（先装 notebook 内核，见下）
3. 选择解释器：`run-agent/.venv`
4. 自上而下 Run → 再对照 `agents/` 当前分层实现
5. 演示绿了再 `feat: …` 提交

可选（在已激活的 `.venv` 里）：

```powershell
pip install ipykernel
python -m ipykernel install --user --name=run-agent --display-name="Python (run-agent)"
```
