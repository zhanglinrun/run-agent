# Examples

Session 扩展示例：`extensions/hello_tool.py` 注册工具，`extensions/prompt_section.py` 添加提示词，`extensions/terminal_status.py` 显示扩展自己的文本状态。

```powershell
run --extension examples/extensions/hello_tool.py --print "Use hello"
run --extension examples/extensions/terminal_status.py
```

普通扩展同步导出 `setup(api)`。MCP、Plan、Permission、Verification 和 Observability 在仓库的 `extensions/` 中单独维护。扩展 UI 使用文本通知、状态与异步对话，不提供旧 Textual 组件接口。

渠道扩展 `gateway_extensions/feishu.py` 导出 `setup_gateway(api)`，由独立 Gateway 宿主管理。凭据、权限和启动方法见 [Gateway 示例](gateway_extensions/README.md)。新版本地 Gateway 验收使用 Python 内存 Adapter；已移除逐行 stdin/stdout Adapter。
