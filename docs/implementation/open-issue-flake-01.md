# 未决问题 01：test_terminal 密钥用例的间歇性失败

状态：**未解决，未定位断言**。本文件只记录已观察事实与分析假设，不把假设写成结论。

## 已观察事实

失败用例：
`tests/redesign/test_terminal.py::test_secret_dialog_does_not_pollute_history_and_timeout_restores_editor`

| 观测 | 次数 |
| --- | --- |
| 全量 `pytest -q` 通过 | 12 |
| 全量 `pytest -q` 失败 | **2**（约 1/6） |
| 单测隔离通过 | 5 |
| 单测隔离 + CPU 负载通过 | 12 |

两次失败都发生在**全量套件**下，且该次运行明显偏慢（58.04s vs 常规约 50s）。
用 `--tb=long` 连续跑 8 次全量仍未能复现，因此**始终没有拿到失败的断言文本**。

## 已排除

- 不是本次改动引入：失败触及的 `terminal.py` 与我的改动无关（我只动过 `scripts/`、
  `docs/`、`mise.toml`、`tests/redesign/test_simulation_support.py`）。
- 不是单纯 CPU 负载：12 次带人工 CPU 负载的隔离运行全部通过。

## 分析假设（未证实，不得据此改代码）

读 `src/run_agent_coding/terminal.py` 得到两条**候选**解释，都需要真实 traceback 才能判定：

1. **超时对话框未被拆除（产品行为）**：`ui.input(timeout=...)`（`terminal.py:83`）在
   0.05s 超时时只放弃调用方的 `await`，返回 None；`_read()`（`terminal.py:205` 起）里
   对话框的生命周期由 `answering`/`timeout_waiter`/`shutdown` 三者决定。若超时发生得比
   `_read` 取到该对话框更早，走的是 `dialog.result.done()` 分支（`:225`，已处理）；否则
   走 `timeout_waiter` 分支。两条路径看起来都被处理了，但**没有一条显式检查“调用方已经
   放弃了这个 future”**。若拆除发生得比用例随后的 `pipe.send_text("/quit\r")` 更晚，那个
   `/quit` 可能被仍打开的对话框吃掉，导致末尾 `wait_for(task, 5)` 超时。
2. **密钥进入主编辑器历史（产品行为）**：用例在 `terminal._dialog_prompt` 被调用的那一刻
   置 `shown` 事件，然后立刻 `send_text("secret-test-value\r")`。从
   `prompt_async()` 被进入，到 prompt_toolkit 真正接管同一条 pipe，之间存在窗口；若文本
   落在窗口内，可能被已被 `typing.cancel()` 的主编辑器缓冲，进而进入
   `terminal.editor.history`，使 `assert "secret-test-value" not in ...history...` 失败。

假设 2 若成立属于**安全问题**（密钥进入历史），不是普通 flake。

## 新证据：本机墙上时钟断言确实会不稳定（已实测）

写 P0-6 支撑时我自己写了一个“延迟至少 0.05s”的断言，它在全量套件下失败、单独跑却通过。
连续跑 30 次后得到确凿数据：

```text
failures: 12 / 30
E   assert 0.046999999998661224 >= 0.05
E:\pythonProject\run-agent\tests\redesign\test_simulation_support.py:50
```

即 `asyncio.sleep(0.05)` 在本平台会**提前约 3ms 返回**（Windows 定时器粒度），于是
“耗时 >= 请求值”这类断言约有 40% 概率失败。

这条证据对本文件主题的意义：它**直接支持“墙钟预算断言是这一失败类别的主因”**这一假设。
`test_terminal` 用例里的 `timeout=0.05` 与三处 `wait_for(..., 5)` 都是同一类断言。
但它仍**不能**证明 `test_terminal` 失败就是同一原因——那只用例的失败断言至今未被捕获。

已处置：我把自己那个断言改为注入式——`ControlledTool` 接受可注入的 `sleep`，测试断言
“请求的延迟是 0.05s”而不断言墙上时钟；另保留一个下限故意放宽到 0.03s 的真实时钟用例
（它仍能抓到“延迟被完全忽略”）。改后同一文件连续 30 次全绿，延迟用例单独 30 次也全绿。

## 处置

按“先复现再修”执行：未拿到真实失败断言前**不得**修改 `src` 或测试。已排入任务 T-045。
若长期无法复现，则保留本文件记录的不确定度，并明确“该用例在 1/6 量级上不确定”，
不得对外声称闸门完全确定。
