# Examples

## Agent extensions

- `extensions/hello_tool.py`：注册自定义工具。
- `extensions/prompt_section.py`：添加稳定的 system-prompt section。
- `extensions/sidebar_status.py`：通过 UI bridge 更新侧边栏状态。

Mem0、MCP、Plan、Permission、Verification 和 Trace Recorder 是仓库级正式可选扩展，位于
`../extensions/`，不再作为 examples 或内置 Session 能力重复维护。

显式路径由调用方信任：

```powershell
uv run run-agent -e examples/extensions/hello_tool.py --print "Use hello"
```

普通扩展同步导出 `setup(api)`；异步工作放进事件/工具/命令 handler。失败的 setup 会回滚该
source 的全部注册，reload 后旧 API generation 会失效。

## Gateway extension

`gateway_extensions/stdin_jsonl.py` 实现 UTF-8 JSONL stdin/stdout adapter，并同步导出
`setup_gateway(api)`：

```powershell
'{"id":"demo-1","conversation_id":"room","text":"Reply with OK"}' |
  uv run run-agent-gateway --extension examples/gateway_extensions/stdin_jsonl.py --cwd .
```

生产渠道 adapter 使用同一 `messages/send/close` 协议；渠道凭证和 SDK 生命周期由 adapter
持有，Agent 会话与调度由 Gateway host 持有。
