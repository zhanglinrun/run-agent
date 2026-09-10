配置键 `timeout_seconds` 统一改名为 `timeout_ms`，单位从秒改为毫秒。

要求：
- `settings.resolve()` 只接受 `timeout_ms`，不再接受旧键；
- 默认值改为 30000 毫秒；
- `client.build()` 也必须改用新键（这是跨文件迁移，不是只改一处）。

不要修改测试。
