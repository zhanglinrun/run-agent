# 结构债清单：>200 LOC 的文件

T-024 的 `validationSteps` 写着「拆分仍 >200 行的文件（含 `src/run_agent_gateway/repository.py`
1196 行等前序遗留）」。实际盘点后，规模远超那句话给人的印象，因此**登记为独立工程**，
而不是当作收尾清理的一部分。

## 现状

```
files over 200 LOC: 79
 total excess lines: 25,830

functions total: 2981
functions over 30 LOC: 370  (12.4%)
```

**函数级违约比文件级更能说明问题。** 下面这十二个尤其突出：

| LOC | 函数 |
|---|---|
| **302** | `src/run_agent_coding/session.py:804 _load` |
| 260 | `src/run_agent_ai/anthropic.py:111 _stream_provider_events` |
| 248 | `src/run_agent_coding/session.py:2415 reload` |
| 247 | `src/run_agent_ai/anthropic.py:122 iterator` |
| 230 | `src/run_agent_coding/session.py:3225 prompt` |
| 226 | `src/run_agent_gateway/repository.py:294 admit` |
| 211 | `extensions/experience/extension.py:57 setup` |
| 205 | `src/run_agent_core/loop.py:139 run_agent_loop` |
| 198 | `src/run_agent_coding/tools.py:190 create_read_tool_definition` |
| 188 | `scripts/validate_distribution.py:242 check_gateway_cli` |
| 180 | `src/run_agent_gateway/controller.py:30 handle` |
| 174 | `src/run_agent_ai/openai_codex.py:504 _codex_provider_events` |

注意其中几个就在**本轮动过的范围内**：`extensions/experience/extension.py:setup`
（211 行）与 `repository.py:admit`（226 行）属于 P5 的交付物 ——
它们的文件大小（268/238）掩盖了真正的问题在函数粒度上。

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

## 已完成的批次

### 批次 0（第一例）：`models_dev_store.py`

| | 前 | 后 |
|---|---|---|
| `models_dev_store.py` | 211 行 | **86 行** |
| `models_dev_refresh.py` | — | **144 行** |
| `refresh_models_dev_catalog` | **92 行** | 拆为 `_reusable` / `_fetch_and_store` / `_fetch_nvidia_filter` / `_build_document` / `_store` / `_request_headers`，均 ≤30 行 |

**seam 的依据**：两个模块回答不同问题 —— store 回答"缓存里有什么"，refresh 回答"要不要取、取回什么"。
两个职责混在一起才产生了那个 92 行的刷新例程。

**导入方只有 2 个**，已同步：`session.py:63` 改为从新模块导入（未留兼容 shim）。

**验证方式值得记录**：这段代码**没有直接测试覆盖**（全仓搜索无命中），
所以"行为不变"只能靠**全量闸门**证明 —— 417 passed、10/10 步、exit 0。
若将来要为它补测试，应从 `refresh_models_dev_catalog` 的四个分支入手：
离线、刷新窗口内、304 not-modified、正常刷新。

### 批次 0（第二例）：`extensions/experience/review.py`

**下一个超限文件是本轮自己造成的** —— T-023 的前台守卫与用量归因把它推到 219 行。
拆分才是诚实的修法，而不是给自己开例外。

| | 前 | 后 |
|---|---|---|
| `review.py` | 219 行 | **174 行** |
| `review_models.py` | — | **34 行** |

`ReviewPolicy` / `ReviewRequest` / `ReviewDecision` 与两个命名空间常量是**数据**，
trigger 与 coordinator 是**行为** —— 同一 seam。名字仍可从 `review` 导入，调用方不受影响。

### seam 的共同规律

两例的 seam 完全一致：**把数据与行为分开，把“回答什么”与“怎么做”分开**。
函数级超限往往是这两个职责混住后的症状，而非独立问题。

### 批次 1（进行中）

已完成：`branch_summary.py` 229→79 + `branch_summary_format.py` 135；
`process_probe.py` 205→154 + `process_probe_windows.py` 60；
`extension_installer.py` 201→110 + `extension_git_source.py` 75；
`models.py` 206→135 + `task_loading.py` 71；`review.py` →181 + `review_models.py` 69。

### ⚠️ 行数测量的陷阱（已踩过）

**`Get-Content | Measure-Object -Line` 不把空行计入**。用它核对文件大小会**系统性低估**，
并且会产出“自信的错数字”—— 我曾据此错误地声称 `review.py` 已修好（实际 202，仍超限）。

**正确做法**：`len(path.read_text(encoding="utf-8").splitlines())`。本轮后续全部改用此法。

### 下一个目标：`storage/handle.py`（真实 209 行）

单个类 `SqliteSessionHandle`，职责混住：

| 组 | 成员 | 行 |
|---|---|---|
| 租约 | `_renew` / `_check` / `closed` | 50–68 |
| 读取 | `read_entries` / `get_head` | 69–76 |
| 条目 | `append_entries` / `fork` | 77–129 |
| **run 生命周期** | `begin_run` / `run_is_revoked` / `complete_run` | **130–169** |
| 上下文 | `record_context` | 170–186 |
| 关闭 | `aclose` / `_close` | 187–209 |

**关键陷阱：这两件事必须同时做。**

`fork`（85–129）是 **45 行**，违反函数级限制。但**仅就地拆分它会把文件从 209 推到约 219** ——
修好函数指标、弄坏文件指标。抽离 run 生命周期组（~40 行）才能让两者同时达标。

**预期**：文件 209 → ~180；函数违规 −2（`fork` 45 行，及新拆片均 ≤30）。

**之后按真实行数**：`ai/stream.py` 212 → `storage/state.py` 225 → `ai/http.py` 229
→ `experience/repository.py` 238。

## 优先级 1：深嵌套流式解析（进行中）

### 已完成

**A. `ai/anthropic.py:111 _stream_provider_events`（260 行 → 拆为 7 个命名部分）**

原形是：外层 260 行函数的**全部**工作只是 `return iterator()`，真正的 247 行在内部
生成器里，嵌套 9 层。浓度从 **0.34 → 0.07**。

**B. `ai/openai_codex.py:504 _codex_provider_events`（174 行 → 分派表）**

同样两个结构病：if/elif 链（导致**单一事件类型无法单独测试**）+ 四个工具追踪集合
在 6 处调用点**重复同一组关键字参数**（导致索引漏更新时静默失败）。

### 已确立的两个可复用修法

1. **分派表替代 if/elif 链**：`_HANDLERS: dict[str, Handler]`，处理器签名为
   `(state, chunk/event) -> tuple[ProviderEvent, ...]`。返回事件元组而非 yield，
   使得**单个事件类型可以单独测试**。
2. **累加器进状态对象**：10 个可变量成为 `@dataclass(slots=True)` 的字段。

### 已验证的收益（两项均全量测试 417 passed）

| 文件 | 最大函数 | 浓度 | 行数 |
|---|---|---|---|
| `anthropic.py` | 260 → 64（且 64 属既有） | 0.34 → 0.07 | 771 → 968 |
| `openai_codex.py` | 174 → 78 | 0.16 → 0.07 | 1081 → 1187 |

**代价是诚实的：两个文件都变大了。** 处理器需要同模块的私有解析助手，
把处理器移到新模块会造成循环导入。**两个文件仍超 200 行**，需要先抽出共享助手
才能继续拆文件。

### 下一个目标（已探明形态，无需重新发现）

**`ai/openai_codex.py:158 _stream_provider_events`（155 行）** —— **与 A 完全同型**：

```
L158 def _stream_provider_events(...)     155 行
L168     """docstring"""
L170     async def iterator():             141 行   ← setup + while True 重试循环
L312     return iterator()                 ← 外壳什么也没做
```

`iterator` 体内：setup（client / cache_key / payload / url）→ `attempt = 0` → `while True:`。

**关键发现：重试循环与 anthropic 的四段构造逐字重复**（HTTP 错误 / 流错误 / 网络错误
各自手写 `retry_delay_seconds` + `provider_retry_event`）。

**因此正确的做法是先抽出共享类型**，而不是再复制一份：

- 在两处提供者之外建 `ai/stream_retry.py`，放 `_Retry`（event + delay）、
  `StreamOutcome`（COMPLETED / ABORTED）、`AttemptResult` 联合类型
- 两侧各自实现一个 `_retry(reason, attempt, data)` 方法 **一次性**构造延迟与事件
  （anthropic 已完成此去重，可作范本：4 处重复→ 1 处）
- 然后把 `_stream_provider_events` 改为真正的异步生成器，拆出
  `_prepare_codex_request` / `_http_error_outcome` / `_network_error_outcome`

**注意 `iterator` 末尾有一个 `except Exception` 兜底**（把未知异常也转成事件）——
拆分时**不得改变这个行为**，但值得单独商権：它与
`run_agent_evals/runner.py` 里“未知异常照常抛出”的做法相反。

### 剩余优先级 1 目标

`core/loop.py:139 run_agent_loop`（205 行，嵌套 5，35 分支）—— 与 A/B 不同型：
这是**循环控制流**而非事件分派，需要先判别哪些步骤可提取。

## 建议做法（需单独排期）

按**风险从低到高**分批，每批都要求先有测试锁定行为：

| 批次 | 对象 | 理由 |
|---|---|---|
| 0 | **先拆函数而非文件** | 370 个超长函数里，多个在已合规文件内；先抽小函数能同时降低文件体积 |
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
