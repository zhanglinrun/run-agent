# 闸门间歇性失败：根因定位（T-001）

**结论：不是超时，是丢输入。** 具体地：对话框超时被取消后，其 prompt_toolkit 输入层
在拆除窗口内仍会消费一行管道输入并丢弃，此时主提示符尚未重新武装（或刚武装但那一行已经没了）。

定位手段是一个判别器：挂死时**再发一次 `/quit`**。

- 第二次生效 → 读取循环活着，第一行被吞（丢输入）
- 第二次无效 → 读取循环已死

实测第二次**瞬间生效（0.00s）**，因此是**丢输入**。

## 一、先说被推翻的假设

| 假设 | 状态 | 证据 |
|---|---|---|
| 负载导致 5 秒截止不够（超时） | **已推翻** | 正常退出耗时 **0.000–0.079 秒**；5 秒是其 **60–250 倍**，负载不可能解释 |
| 密钥泄露进历史 / 输出 | 已推翻（前序） | 三条断言全过 |
| 输入循环/终端进程死掉 | **已推翻** | 第二次 `/quit` 以 0.00s 生效 |

## 二、决定性实验

### 实验 1：隔离复现率与退出延迟

`.run/verify/dialog_timeout_probe.py`，忠实复刻
`tests/redesign/test_terminal_dialog_timeout.py`，唯一区别是把最后的
`await asyncio.wait_for(running, 5)` 换成**无界观测**（grace 20s）。不修改 `terminal.py` 与任何测试。

```
iterations=200 grace=20.0s timeout=0.5s
  [ 18] HANG at 20.00s  {'awaiting': 'terminal.py:351:run', 'live_task_count': '7',
                         'coroutines': 'Application._poll_output_size | Event.wait | Queue.get |
                                        SqliteSessionHandle._renew | SqliteTelemetrySink._drain |
                                        Terminal._prompt | Terminal.run',
                         'second_quit_exited': 'True', 'second_quit_latency': '0.00s'}

=== summary ===
  completed                199/200  (99.5%)
  HANG                     1/200  (0.5%)
  clean exit latency: min=0.000s max=0.079s
```

**三个关键读数**：

1. **正常退出只要 0.00–0.08 秒** —— 5 秒截止极其宽松，"慢" 不是原因。
2. 挂死时 **`Terminal._prompt` 在活跃协程列表中** —— 主提示符**正在读**。
3. **`second_quit_exited: True` / `second_quit_latency: 0.00s`** —— 再发一行立刻退出。

第 2 与第 3 条合起来就是结论：**读取循环是活的，主提示符已武装，但第一条 `/quit` 不见了。**

### 实验 2：全量套件的失败分布（复刻 verify.py 顺序）

先跑 compileall + ruff + mypy，再跑套件，共 6 轮：

```
--- run 1 (exit=0) ---  tail: 334 passed, 2 skipped, 1 warning in 73.95s
--- run 2 (exit=0) ---  tail: 334 passed, 2 skipped, 1 warning in 77.39s
--- run 3 (exit=0) ---  tail: 334 passed, 2 skipped, 1 warning in 71.75s
--- run 4 (exit=0) ---  tail: 334 passed, 2 skipped, 1 warning in 76.59s
--- run 5 (exit=0) ---  tail: 334 passed, 2 skipped, 1 warning in 76.29s
--- run 6 (exit=0) ---  tail: 334 passed, 2 skipped, 1 warning in 72.42s
```

6/6 全绿。但**紧接着跑真正的 verifyCommand，第 3 次就复现了**：

```
=== verify.py run 1 : exit=0 ===   All 10 steps passed.
=== verify.py run 2 : exit=0 ===   All 10 steps passed.
=== verify.py run 3 : exit=1 ===
SKIPPED [1] tests\redesign\test_skill_packages.py:128: This host cannot create symlinks
FAILED tests/redesign/test_terminal_dialog_timeout.py::test_a_timed_out_dialog_returns_the_terminal_to_the_main_prompt
1 failed, 333 passed, 2 skipped, 1 warning in 78.33s (0:01:18)
--- tests: exit 1 in 79.4s
FAILED at step 'tests'. Later steps were not run.
```

**因此间歇仍然存在**，隔离约 **0.5%**，全量 verify.py 下显著更高（3 次中 1 次）。
早先观察到的多个失败用例（secret dialog、process_supervisor）是**同一丢输入症状在不同调用点的表现**，
但此点尚未逐一举证，不得当作已证实。

## 三、缺陷位置（已证实）

### 3.1 代码结构

`src/run_agent_coding/terminal.py` 的 `Terminal._read`（203–256 行）在对话框期间
**同时存在两个 prompt_toolkit 读取器**，二者共享同一个输入管道：

- `typing = asyncio.create_task(self._prompt("❯ "))` —— 主提示符（第 206 行）
- `answering = asyncio.create_task(self._dialog_prompt(dialog))` —— 对话框（第 224 行）

超时时序（第 231–247 行）：

```
timeout_waiter = asyncio.create_task(dialog_done())
done, _ = await asyncio.wait([answering, timeout_waiter, shutdown], FIRST_COMPLETED)
# 超时：返回的是 timeout_waiter，answering 不在 done 里
finally:
    answering.cancel()          # 取消对话 Application —— 但拆除是异步的
    timeout_waiter.cancel()
    await asyncio.gather(answering, timeout_waiter, return_exceptions=True)
self._prefill = saved           # 循环回到顶部，重新武装主提示符
```

### 3.2 时序已被证实（reader_attribution_probe.py）

给两个读取器加追踪后，**两次挂死呈现完全相同的时序**（300 迭代，2 次挂死，0.67%）：

```
--- HANG at iteration 171 ---
    +  0.684s  main    enter '❯ '
    +  0.700s  main    RAISED CancelledError
    +  0.700s  dialog  enter 'API key'
    +  0.700s  dialog  RETURNED 'value'
    +  0.700s  main    enter '❯ '
    +  0.700s  main    RAISED CancelledError
    +  0.700s  dialog  enter 'Timeout'
    +  1.200s  test    SEND /quit
    +  1.200s  dialog  RAISED CancelledError
    +  1.200s  main    enter '❯ '
    second_quit_exited=True

=== summary ===
  completed                298/300  (99.33%)
  HANG                     2/300  (0.67%)
```

（iteration 258 的轨迹与上表同构，仅差 0.016s。）

**关键读数**：`timeout=0.5` 且对话框在 0.700 打开，所以超时应发生在 **1.200**。
而 `SEND /quit`（调用方已恢复执行）、`dialog RAISED CancelledError`、`main enter`
**全部落在同一瞬间 1.200**。

### 3.3 结论：释放时序错误

**`ui.input(timeout=...)` 在对话框读取器拆除完成、主提示符重新武装之前，就把控制权还给了调用方。**
任何落在这个窗口内的输入都会被垂死的对话框读取器消费并丢弃。

这就解释了 T-001 的两个看似矛盾的读数：

| T-001 观察到 | 本质 |
|---|---|
| 挂死时 `Terminal._prompt` 在活跃协程列表里 | 主提示符**刚刚**被武装（同一瞬间），但那一行已经丢了 |
| 第二次 `/quit` 以 0.00s 生效 | 此时主提示符已是唯一读者，所以立刻生效 |

### 3.4 已知 / 未知（更新）

| | 内容 |
|---|---|
| **已证实** | 失败率（隔离 0.67%）；退出仅需 0.000–0.079s；挂死时读取循环活着；第二次输入 0.00s 生效；**释放时序：调用方先于读取器拆除而恢复** |
| **尚未证实** | 那一行具体是被对话框 Application 消费还是丢弃在管道交接中（两者对修复方案无影响，但不得声称其一） |
| **未知** | 全量套件下失败率从 0.67% 升到 33% 的放大机制 |

> 探针中的 `dialog_returned_any_value` 标志是坏的：它把第一个**被回答**的对话框也算进去，
> 因此 `2/2` **不能**作为“对话框吃掉了那一行”的证据。真正的证据是上面那条时序。

## 五、修复与验证

### 5.1 修复（T-002）

根因是释放时序，所以修复就落在时序上：**超时路径必须等对话框读取器真正结束，才允许调用方恢复**。

`src/run_agent_coding/terminal.py` 三处改动：

1. `_Dialog` 增加一个 `retired: asyncio.Event | None` 握手位。
2. `TerminalUi.input` 超时不再直接 `return None`，而是 `await asyncio.shield(retired.wait())` 后再返回。
3. `Terminal._read` 在对话框读取器拆除完成处（`gather(answering, timeout_waiter)` 之后）
   置位 `retired`；对话框尚未被接管就已经超时的那条路径也置位，避免调用方等一个永不到来的信号。

LOC 影响：`_Dialog` +1 行，`input` +4 行，`_read` +4 行。未触及读取器结构，未调整任何 deadline。

### 5.2 回归测试（确定性）

`tests/redesign/test_terminal_dialog_handoff.py`。**不靠碰运气迭代**：把对话框读取器的
拆除拖长（延迟 0.2s），使那个 0.67% 的窗口变成 **100%**，于是缺陷可以确定性判定。

- **判别性用例**：`test_a_timed_out_dialog_does_not_release_the_caller_while_still_reading`
  —— 断言“释放时对话框读取器必须已经结束”。修复前 **3/3 确定性失败**，修复后通过。
- **护栏用例**：`test_a_line_written_after_a_timed_out_dialog_reaches_the_main_prompt`
  —— 钉住用户可见属性。**诚实标注：它修复前就已通过**（只拉长取消之后的窗口，
  那一行会留在管道里而不是被吃掉），所以它是护栏而**不是判别器**，不被声称为修复证据。

### 5.3 验证证据

**基线（修复前）**：`dialog_timeout_probe.py` 300 迭代 → `2/300 HANG (0.67%)`。

**修复后**，同一探针同一参数：

```
iterations=300 grace=15.0s timeout=0.5s

=== summary ===
  completed                300/300  (100.0%)
  clean exit latency: min=0.000s max=0.078s
```

**闸门（修复前 3 次里 1 次失败）**：连续 5 次全量 `verify.py`

```
run 1 exit=0 :: 336 passed, 2 skipped ;; All 10 steps passed.
run 2 exit=0 :: 336 passed, 2 skipped ;; All 10 steps passed.
run 3 exit=0 :: 336 passed, 2 skipped ;; All 10 steps passed.
run 4 exit=0 :: 336 passed, 2 skipped ;; All 10 steps passed.
run 5 exit=0 :: 336 passed, 2 skipped ;; All 10 steps passed.
```

### 5.4 仍未证实

- `process_supervisor` 的同类症状**本次未复现也未修复**，不得当作已解决。
- 全量套件把 0.67% 放大到 33% 的机制仍未定位；修复后 5/5 全绿是观测，不是机制解释。

## 六、遗留事项

1. **`process_supervisor` 的同类症状尚未处理**。它从未被本次复现，也没有被修复。
   在另行举证之前，不得当作已解决，也不得当作与本次同一根因。
2. **全量套件放大 0.67% → 33% 的机制未知**。修复后 5/5 全绿只是观测；
   如果未来再出现间歇，应从这条线索入手，而不是重跑已有结论。
3. **其他约 50 处 5 秒墙钟 deadline 未动**。它们不是本次故障的原因
   （正常退出只需 0.000–0.079 秒），但如果将来出现真正的慢路径，它们会成为嫌疑。
   届时仍应先把根因分清超时还是挂死，再决定是否收敛为共享常量。
4. **两个诊断探针位于 `.run/verify/`（已 gitignore）**，属于一次性诊断产物，
   方法已完整写入本文件，需要时可据本文件重建。
