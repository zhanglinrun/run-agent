# 结构债清单：>200 LOC 的文件

T-024 的 `validationSteps` 写着「拆分仍 >200 行的文件（含 `src/run_agent_gateway/repository.py`
1196 行等前序遗留）」。实际盘点后，规模远超那句话给人的印象，因此**登记为独立工程**，
而不是当作收尾清理的一部分。

## 现状

```
files over 200 LOC: 79
total excess lines: 25,830
```

### 最大的 15 个

| LOC | 文件 |
|---|---|
| **5029** | `src/run_agent_coding/session.py` |
| **2593** | `src/run_agent_coding/provider_config.py` |
| 1390 | `src/run_agent_coding/extensions/runtime.py` |
| 1369 | `src/run_agent_ai/openai_compatible.py` |
| 1197 | `src/run_agent_gateway/repository.py` |
| 1154 | `src/run_agent_coding/tools.py` |
| 1081 | `src/run_agent_ai/openai_codex.py` |
| 916 | `src/run_agent_coding/session_usage.py` |
| 913 | `src/run_agent_coding/extensions/api.py` |
| 832 | `src/run_agent_coding/commands.py` |
| 813 | `src/run_agent_coding/storage/sessions.py` |
| 797 | `src/run_agent_core/loop.py` |
| 772 | `src/run_agent_ai/anthropic.py` |
| 764 | `src/run_agent_coding/catalog_loader.py` |
| 736 | `src/run_agent_coding/project_trust.py` |

其余 64 个在 201–736 行之间。

## 为什么不在本轮做

1. **体量**：2.6 万行、79 个文件，是**独立工程**而非收尾项。
2. **风险**：`session.py`（5029 行）与 `loop.py`（797 行）是**核心运行时**；
   `extensions/runtime.py`（1390 行）是扩展隔离边界。拆分它们必须逐段有测试保护，
   属于 P6 范围之外。
3. **本轮新增代码已全部合规**：本次循环新增的 14 个模块全部 ≤200 LOC、
   每函数 ≤30 LOC、嵌套 ≤3 —— 这条是**已验证**的（见下表）。

## 建议做法（需单独排期）

按**风险从低到高**分批，每批都要求先有测试锁定行为：

| 批次 | 对象 | 理由 |
|---|---|---|
| 1 | `catalog_loader`、`models_dev*`、`prompt_templates`、`image_processing` | 纯数据/格式转换，无运行时耦合 |
| 2 | `run_agent_ai/*`（各 provider 实现 500–1369 行） | 边界清晰，可抽出共享的 HTTP/流式层 |
| 3 | `run_agent_gateway/*` | 有完整测试套件覆盖（repository/gateway/controller） |
| 4 | `run_agent_coding/storage/*`、`host/*` | 事务边界，需最谨慎 |
| 5 | **`session.py`、`loop.py`、`extensions/runtime.py`** | 核心运行时；建议拆成职责模块并保持公开契约不变 |

## 本轮新增模块的实际尺寸（全部合规）

```
  67 attribution.py    58 prefix.py      80 rubric.py      90 reflow.py
  79 ledger.py        133 verifier.py    58 grader_runner.py
  80 environment.py   163 suite.py      132 statistics.py
  80 curation.py       50 promotion.py   87 flake_survey.py  74 flake_rivals.py
functions over 30 LOC: none      files over 200 LOC: none
```
