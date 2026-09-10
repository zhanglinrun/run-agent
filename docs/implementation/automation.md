# 自动化闸门

规范的单一闸门是 `scripts/verify.py`。它同时是本地开发入口、本地发行就绪入口和 CI 的等价物。

## 用法

```powershell
.\.venv\Scripts\python.exe scripts\verify.py            # 全量闸门（10 步）
.\.venv\Scripts\python.exe scripts\verify.py --fast      # 变更文件子集，供本地迭代
.\.venv\Scripts\python.exe scripts\verify.py --skip-dist # 全量但跳过构建/安装步骤
```

退出码：0 表示全部步骤通过；非零表示第一个失败步骤的退出码，且后续步骤不执行。

> 用 PowerShell 调用时不要接 `| Select-Object -First N`。该 cmdlet 会提前终止管道，
> `$LASTEXITCODE` 会变成无意义的值（实测出现 `-1`）。需要看输出请重定向到文件再读。

## 步骤清单（全量）

| # | 步骤 | 命令 |
| --- | --- | --- |
| 1 | compile | `python -m compileall -q src extensions tests` |
| 2 | format | `python -m ruff format --check .` |
| 3 | lint | `python -m ruff check .` |
| 4 | types | `python -m mypy` |
| 5 | tests | `python -m pytest -q` |
| 6 | build | `python -m build --outdir .run/verify/dist` |
| 7 | wheel-audit | `scripts/verifylib/wheelaudit.py --dist .run/verify/dist` |
| 8 | install | `scripts/verifylib/distcheck.py install`（干净 venv + wheel） |
| 9 | pip-check | `.run/verify/env/Scripts/python.exe -m pip check` |
| 10 | dist-check | `scripts/validate_distribution.py`（仓库外验证安装产物） |

实测：全量 10 步通过，退出码 0，约 70 秒。

## 与 CI 的等价性

对照 `.github/workflows/ci.yml` 的每一个 step：

| CI step | 本地对应 | 状态 |
| --- | --- | --- |
| `actions/checkout@v4` | — | NOT-LOCALLY-VERIFIABLE（远端 runner 基础设施） |
| `actions/setup-python@v5` + `cache: pip` | — | NOT-LOCALLY-VERIFIABLE；本地为 `.venv` 的 Python 3.12.4 |
| `pip install -e ".[dev,feishu]"` | — | NOT-LOCALLY-VERIFIABLE 为独立步骤；本地检出环境的 `.venv` 已含 editable 安装与 dev、feishu extras |
| `python -m compileall -q src extensions tests` | compile | 等价（命令逐字相同） |
| `ruff format --check .` | format | 等价（命令逐字相同） |
| `ruff check .` | lint | 等价（命令逐字相同） |
| `mypy` | types | 等价；本地走 `.mypy_cache` 增量 |
| `python -m pytest -q` | tests | 等价（两条命令都收集 196 项） |
| `pip wheel . --no-deps --wheel-dir .run/wheels` | build | 更强：本地构建 wheel + sdist |
| wheel layout audit（heredoc） | wheel-audit | 等价，另把 `run_agent_entry.py` 加入必需集合（计划 §4.4.2 要求入口进 wheel） |
| `python -m pip check` | pip-check | 更严格：在干净安装环境执行，而非开发环境 |
| — | install | 仅本地：全新 venv 安装 wheel，证明产物在源码树外可用 |
| — | dist-check | 仅本地，但为计划验收所要求：复用既有 `scripts/validate_distribution.py` |

**不得宣称已验证的项**：GitHub 托管 runner 的实际行为、远端 pip 缓存行为、`ubuntu-latest`
平台差异。本套件只在 Windows 上执行；POSIX 专属断言在 Windows 上 skip（当前 2 项），
其证据来自前序 checkpoint 的 WSL 运行记录。

## 性能与缓存

- `--fast` 静态三步实测 0.39 秒（format 0.09 / lint 0.07 / types 0.24，mypy 走增量缓存）。
  加上受影响测试子集实测约 1.1 秒，合计约 1.5 秒，满足本地小改动的秒级反馈目标。
- 干净安装环境按 wheel 的 sha256 缓存：命中时打印 `cache hit` 且该步 0.1 秒；未命中才重建，
  实测冷装 13.2 秒。仅改动 `scripts/` 不会改变 wheel，因此会命中缓存。
- `ruff format` 覆盖 Markdown：ruff 0.16 会格式化 Markdown 代码围栏里的 Python，
  所以 `docs/**/*.md` 也在闸门范围内，不是路径误报。
- 步骤串行执行是刻意选择而非遗漏：测试套件共享同一个真实 SQLite 状态目录与单写者，
  并行步骤会竞争真实持久状态。理由写在 `scripts/verifylib/runner.py` 的模块 docstring 里。

## 偏离记录：mise-first

通用规则要求 mise-first。本仓库不引入 mise，理由由用户明确给出：

> 「不做这个mise啊，我看计划里面没有啊，别自己乱加要求」

即：`study/简历五条/00-RunAgent完整改进执行计划.md` 未把 mise 列为交付要求，
因此不新增该外部依赖。规范入口由 `scripts/verify.py` + `.venv` 承担，效果等价：
单条命令、与 CI 逐条等价、可复现、可缓存。

## 已知缺口

- 没有并行测试执行。上文的共享持久状态是原因；在隔离每个测试的 SQLite 目录之前不启用。
- 远端 CI 在本次运行中未被触发（不 push），因此 CI 的通过状态未被实证，只有闸门等价性被实证。
