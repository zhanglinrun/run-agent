# 检查点 06：通用扩展清理回调

日期：2026-09-10。完整改造继续执行。

- 增加 api.register_disposer(async_callback)。回调归属于已有 source_id/generation，可在 setup 或活动实例中注册，最多每源 64 个。
- 同一源按获取顺序逆序清理；不同源并行清理。setup 失败的源在宿主发布存活扩展前执行清理，reload 和退出清理各自旧实例，每个注册只执行一次。
- 默认等待 1 秒，再请求取消并短暂收敛。仍不退出的任务保留句柄并报告；取消或异常不会跳过后续可执行的清理。旧 API 在清理期间仍失效，回调应释放已捕获的本地资源。
- RuntimeCloseResult 增加未完成 disposer 数与清理错误；Session 关闭与 reload 诊断显示这些结果。Provider 清理异常时仍尝试通用回调。

验证：93 项新版测试通过，1 项符号链接测试因本机 Windows 权限限制跳过。mypy 检查 149 个源文件、Ruff 通过。wheel/sdist 构建和源码外新环境安装验证通过，安装包验证覆盖 disposer 在 reload 时运行。没有真实模型调用。

证据：disposer-tests.xml、distribution-check-06.json；安装环境 .run/redesign/install-env-06。数据库仍为 schema 5，无旧版兼容。

尚未完成：resource provider 与全部贡献固定、快照加速恢复、空闲扩展消息的宿主投递、Gateway 持久调度和 Outbox、experience、独立评测、真实模型实验与最终清理验收。完整任务清单仍以 requirements.json 为准。
