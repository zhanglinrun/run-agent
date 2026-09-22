Fix `config.resolve()` so only a missing key falls back to its default. Explicit `None`, an empty string, and numeric zero are intentional values and must be preserved. Do not modify tests.
