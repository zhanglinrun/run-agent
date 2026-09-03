# 受控 Skills 自进化与评测

更新日期：2026-09-03

Run Agent 的“自进化”不是模型修改权重或 Runtime 源码，而是把可复用任务经验沉淀为 Skill。为了避免一次反馈直接污染后续任务，在线流程采用 Candidate-first 发布模型。实现位于 `agents/evolution/`，由交互式 `agents/app` 门面触发，不进入 `AgentCore`。

## 1. 发布流程

```text
assistant response
  -> pending window
  -> next user feedback
  -> candidate extraction
  -> add / merge / discard proposal
  -> .run/skill-evolution/candidates/<candidate_id>/candidate.json
  -> replay gate
  -> boundary gate
  -> retention gate
  -> active Skill promotion
```

`agents/evolution/lifecycle.py` 负责从用户后续反馈中抽取候选并进行 add/merge/discard 维护决策。维护结果不再直接修改 active Skill，而是通过 `agents/evolution/candidates.py` 写入 Candidate Registry。

## 2. Candidate Artifact

```text
.run/skill-evolution/
└── candidates/<candidate_id>/candidate.json
```

Candidate 保存：

- proposed action：add 或 merge；
- target Skill 和 target scope；
- 候选 description / instructions / when_to_use / evidence；
- 维护器决策与创建时间；
- 当前状态：pending_evaluation、rejected、activation_failed 或 promoted；
- promotion evidence。

## 3. 三类晋升门禁

### Replay gate

在冻结的历史对话样本上比较当前 Skill 与候选 Skill，检查可观察的规则、回答质量和 hard failure。

### Boundary gate

使用不应触发该 Skill 的邻近任务，检查候选是否扩大触发范围、引入错误工作流或越权工具行为。

### Retention gate

在已有健康任务集合上确认候选没有造成回归。不能只证明候选在产生它的那条反馈上表现更好。

只有三类门禁全部通过且 `hard_failures == 0`，`promote_candidate()` 才会通过既有的 `create_skill_file()` / `evolve_skill_file()` 写路径更新 active Skill；失败的激活会记录为 `activation_failed`，不会报告为晋升成功。

## 4. 现有评测能力

`agents/evolution/evaluator.py` 已包含：

- lineage 聚合；
- frozen replay pool 和稳定 split；
- programmatic / LLM binary rules；
- usage gate；
- status gate；
- staged candidate replay variants；
- evidence-gated active Skill promotion。

这些能力使用同一 Candidate Registry；评测只处理已显式 staged 的候选，不再生成不会进入 active Skill 的临时变体产物。

Benchmark / Coding / SWE-bench campaign 默认关闭 Skill 自进化，避免跨 case 污染；只有显式开启相关开关时才参与实验。

## 5. 推荐实验

每个候选至少报告：

- replay / boundary / retention 样本数；
- current 与 candidate 的通过率和 hard failures；
- score delta；
- 错误触发率；
- retained task regression rate；
- candidate -> evidence gates -> active 的完整 provenance。

## 6. 当前边界

- 已实现 candidate staging、三门禁 evidence contract 和 active Skill promotion。
- `/skill-eval` 同时评估 pending candidates 与 active lineage，并把激活结果写回 candidate artifact。
- 长期在线流量监控和自动 rollback 不在当前范围内，因此简历中应写“受控候选晋升”，不要写“全自动线上发布回滚”。
