# Bootstrap 审计（pi-config §0 八项）—— P6 范围

依据：`docs/implementation/p6-execution-plan.md` 阶段 A 的门禁要求「接线完成前不新建评测模块」，
以及 §0「RED 之前 bootstrap 必须完成」。

审计时间点：T-003（T-001/T-002 已恢复闸门确定性之后）。

## 0. 闸门步骤耗时剖面（判断「并行是否适用」的依据）

```
--- compile: exit 0 in 0.1s
--- format: exit 0 in 0.1s
--- lint: exit 0 in 0.1s
--- types: exit 0 in 0.3s
--- tests: exit 0 in 71.5s
--- build: exit 0 in 8.1s
--- wheel-audit: exit 0 in 0.1s
--- install: exit 0 in 0.1s
--- pip-check: exit 0 in 0.4s
--- dist-check: exit 0 in 9.9s
All 10 steps passed.
```

| 分组 | 合计 | 占比 |
|---|---|---|
| 静态（format/lint/types） | **0.6s** | <1% |
| **tests** | **71.5s** | **79%** |
| 分发（build→…→dist-check） | 18.6s | 21% |

## 一、八项逐条

### 1. 唯一 gate 来源 —— ✅

`scripts/verify.py` 是唯一权威闸门；`mise.toml` 的 6 个 verb 任务全部委托 `scripts/gate.py`
再调 `verify.py`。`--only` 接受 verb 别名（`typecheck`/`test`）以免出现第二套步骤名。

### 2. 本地开发套件（亚秒到数秒） —— ✅

`verify.py --fast` 走变更子集。实测静态步骤 **0.1s**。

### 3. 本地发布就绪套件（全量、确定性） —— ✅（本轮才成立）

T-001/T-002 之前它**不确定**：`verify.py` 3 次里 1 次红。修复后连续 **6 次退出码 0**。
详见 `docs/implementation/flake-root-cause.md`。

### 4. 变更文件/受影响执行（或已文档化的安全回退） —— ✅

`verifylib/affected.py` 选择变更文件；**高爆破半径路径会自动升级为全量套件**，实测：

```
note: changed files: 2
note: change touches high blast radius paths; falling back to the full suite
```

这是「已文档化的安全回退」，不是静默降级。

### 5. 确定性缓存（或已文档化的无缓存回退） —— ✅（本轮首次实测）

`install` 步骤按 wheel 的 sha256 缓存干净环境，并**打印命中/未命中**。实测连续两次：

```
cache hit: env already holds run_agent_harness-0.5.0-py3-none-any.whl (b6ebaa680c6d)
--- install: exit 0 in 0.1s
```

`types` 走 `.mypy_cache` 增量（0.3s）。缓存键是内容哈希，确定性成立。

### 6. 适用处安全并行 —— ⚠️ 已测量，判定为**当前不适用**

实测依据而非判断：

- 唯一可并行的独立组（format/lint/types）合计 **0.6s**，并行收益 <1s；
- 唯一值得并行的是 **tests（71.5s，79%）**，但该套件含**时序敏感的异步测试**与真实
  子进程/数据库夹具，并行化（pytest-xdist）**不安全** —— T-001/T-002 刚修掉的正是
  这一类非确定性；
- 分发步骤是依赖链（build → wheel-audit → install → pip-check/dist-check），不可并行。

**结论**：不作为缺口处理，但也不是「已满足」的笼统声明 —— 是**测过之后判定的不适用**。
若将来 tests 占比下降或套件改为并行安全，应重新评估。

### 7. 匹配的 GitHub Agentic Workflow（只读 agent + 受保护写 job） —— ❌ **缺失**

`ci.yml` 是普通 CI：`permissions: contents: read`（最小权限已满足），
但**没有任何 agent job**。按 §0 第 7 项，这是真实缺口，未被满足。

### 8. 已验证的本地 ↔ GitHub 对齐 —— ⚠️ 本轮修复了实质差异，环境差异仍不可本地验证

## 二、对齐差异：审计前 → 审计后

审计前 `ci.yml` 自己复制了一套步骤，与 `verify.py` 存在差异：

| verify.py | 审计前 ci.yml | 差异性质 |
|---|---|---|
| compile / format / lint / types / tests | 同名步骤 | ✅ 对齐 |
| build（wheel **+ sdist**） | `pip wheel`（仅 wheel） | ⚠️ 不等价 |
| wheel-audit → `verifylib/wheelaudit.py` | **内联 heredoc 副本** | ⚠️ **重复实现，可漂移** |
| **install**（干净 venv 装 wheel） | **无** | ❌ 缺口 |
| pip-check（**干净 env 内**） | dev env 内 | ⚠️ 不等价 |
| **dist-check**（`validate_distribution.py`） | **无** | ❌ 缺口 |

**修复方式不是「把缺的补进 CI」，而是消灭第二份实现**：让 CI 跑同一个闸门本体。

```yaml
      - name: Gate
        run: python scripts/verify.py
```

收益：

- 内联副本删除 → **零漂移**：本地与 CI 跑的是**同一份文件**；
- 两个缺失步骤（install、dist-check）**自动**被 CI 覆盖；
- build 产物路径、pip-check 环境随之与本地一致。

`contents: read` 不变 —— **未给 workflows 增加任何写权限**。

## 三、仍未验证的部分（不得声称为已对齐）

1. **环境差异无法在本地验证**：CI 是 `ubuntu-latest` + Python 3.12，本机是 Windows。
   已知具体差异：本机 **无法创建符号链接**，因此
   `tests/redesign/test_skill_packages.py:128` 在本地被 **skip**，在 ubuntu 上会真正执行。
   这一条只有在 GitHub 上跑过一次才能确认。
2. **本轮改动尚未在 GitHub 上运行过**。`ci.yml` 的修改是本地编辑 + 本地等价命令验证，
   **不是** CI 实跑结果。按「不确定即未完成」的口径，第 8 项只能算**部分满足**。
3. **第 7 项（Agentic Workflow）缺失未处理**，需要决定：补齐，或明确降级并记录理由。

## 四、本地等价命令验证

CI 现在执行的两条命令，本地等价跑过：

```
python -m pip install -e ".[dev,feishu]"   # dev extras 含 build/mypy/pytest/pytest-asyncio/ruff
python scripts/verify.py                   # 连续 6 次 exit 0，10/10 步
```

`distcheck` 的动作在本地全部实测可跑：`build`（8.1s）、`wheel-audit`（0.1s）、
`install`（0.1s，缓存命中）、`pip-check`（0.4s）、`dist-check`（9.9s）。
