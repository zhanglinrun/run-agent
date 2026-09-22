DEFAULTS: dict[str, object] = {"mode": "safe", "plugins": ["core", "logging"]}


def resolve(
    file_values: dict[str, object], overrides: dict[str, object]
) -> dict[str, object]:
    result = {**DEFAULTS, **file_values, **overrides}
    plugins: list[object] = []
    for source in (DEFAULTS, file_values, overrides):
        for plugin in source.get("plugins", []):
            if plugin not in plugins:
                plugins.append(plugin)
    result["plugins"] = plugins
    return result
