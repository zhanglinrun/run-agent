# 自动化闸门

规范的单一闸门是 `scripts/verify.py`。它同时是本地开发入口、本地发行就绪入口和 CI 的等价物。

## 用法

```powershell
mise run verify                                        # 全量闸门（推荐入口）
mise run verify-fast                                   # 变更文件子集，供本地迭代
mise run typecheck    # 或 lint / format / compile / test / build 单 verb

.\.venv\Scripts\python.exe scripts\verify.py             # 全量闸门（10 步）
.\.venv\Scripts\python.exe scripts\verify.py --fast       # 变更文件子集
.\.venv\Scripts\python.exe scripts\verify.py --only lint,types   # 只跑指定步骤
.\.venv\Scripts\python.exe scripts\verify.py --skip-dist  # 全量但跳过构建/安装
```

`--only` 接受步骤名或 CI verb 别名（`typecheck`→`types`、`test`→`tests`）；选中
`wheel-audit`/`install`/`pip-check`/`dist-check` 时会自动带上 `build`。

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

## mise 任务（verb 级闸门）

`mise.toml` 定义 8 个任务，其中 6 个使用 CI 钩子会发现的 verb 名（`compile`、`format`、
`lint`、`typecheck`、`test`、`build`），每个都委托给同一个闸门：

```powershell
mise run typecheck     # -> python scripts/gate.py --only typecheck
mise run verify        # -> 完整 10 步闸门
mise run verify-fast   # -> 变更文件子集
```

`mise run typecheck` / `lint` / `format` / `compile` / `test` / `build` 实测全部 exit 0
（`test` 194 passed / 2 skipped，耗时 55.2s；`build` 含 wheel 布局审计，8.6s）。

### 为什么要 `scripts/gate.py` 这一层

mise 的 `python` 解析到 PATH 上的解释器（本机是 `E:\Anaconda\python.exe`），它**没有**项目
依赖。`scripts/gate.py` 只依赖标准库，因此任何 Python 都能启动它；它再定位 `.venv` 并调用
`scripts/verify.py`，使检查始终在项目自己的解释器里执行。这样 verb 任务与闸门是**同一个**
实现，不存在第二套检查。

### mise 的安装方式（首次）

`winget install jdx.mise` **失败**：`InternetOpenUrl() failed 0x80072efd`，`--proxy` 也不被该
winget 构建接受，且本机 `127.0.0.1:7897` 代理下 curl 对任何 HTTPS 主机都报 schannel 握手失败。
实际可行的是**直连**：`curl --noproxy "*"` 从 GitHub 直接下载（43.5 MB，4.6s）。

落地位置与解析方式：

| 项 | 值 |
| --- | --- |
| 二进制 | `%LOCALAPPDATA%\Programs\mise\bin\mise.exe`（v2026.8.5 windows-x64） |
| 用户 PATH | 已追加该 `bin` 目录（后续会话生效） |
| 当前进程解析 | `%APPDATA%\npm\mise.exe` —— 指向真二进制的**硬链接** |

### 为什么必须是硬链接，而不能是 `.cmd` 转发脚本

最初的方案是在 `%APPDATA%\npm` 放一个 `mise.cmd` 转发脚本（该目录已在 Pi 进程的 PATH 上）。
**这不行**，而且失败形态与原始故障完全一样：

CI 执行器的 `spawn` 调用（`lib/ci/runner.ts`）：

```ts
spawn(argv[0], argv.slice(1), { cwd, shell: false })
```

注意 `shell: false`。在 Windows 上，Node 的 `spawn` 不带 shell 时**只解析 `.exe`，不会
解析 `.cmd`/`.bat`**（CVE-2024-27980 之后就如此）。因此 `spawn("mise", ...)` 看到
`mise.cmd` 时直接报 `ENOENT`：

```
ERROR code= ENOENT msg= spawn mise ENOENT
close -4058
```

`-4058` 正是最初那条 CI 失败里四个检查的退出码。所以 `mise.cmd` 会让钩子看起来“装了 mise
但依旧 ENOENT”。

改为硬链接 `%APPDATA%\npm\mise.exe` → 真二进制后，用同样的 `shell: false` 路径实测：

```text
spawn('mise','--version')            -> {"exit":0,"error":null,"out":"2026.8.5 windows-x64 ..."}
spawn('mise','tasks','ls','--json')  -> {"exit":0,"names":["build","compile","format","lint",
                                         "test","typecheck","verify","verify-fast"]}
spawn('mise','run','<verb>')         -> exit=0  （typecheck/lint/format/compile/test/build 全部）
```

硬链接与真二进制是同一个文件，不占额外磁盘（实测 145,616,896 字节，与源文件一致）。
复现脚本：`.run/verify/mise-probe.js`（位于 `.gitignore` 内，仅作本机诊断用）。

（历史：用户最初明确拒绝引入 mise，理由是计划未把它列为交付要求；随后 CI 钩子自动发现的
4 条命令全被 `mise exec --` 包裹而失败，用户重新裁定「安装 mise + mise.toml 委托给闸门」。
本节记录的是最终状态。）

## 已知缺口

- 没有并行测试执行。上文的共享持久状态是原因；在隔离每个测试的 SQLite 目录之前不启用。
- 远端 CI 在本次运行中未被触发（不 push），因此 CI 的通过状态未被实证，只有闸门等价性被实证。
