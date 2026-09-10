`config.resolve(overrides)` 应当以 `defaults.py` 里的默认值为基准，再用 overrides 覆盖。
现在未提供的项会退化成 0 或空，而不是默认值。修复它，不要修改 defaults.py 的取值，也不要修改测试。
