"""Distribution entry point. Business behavior belongs to the selected host."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version


def main(argv: list[str] | None = None) -> int:
    """Start the Coding, Gateway or evaluation host without cross-layer imports."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        try:
            release = version("run-agent-harness")
        except PackageNotFoundError:
            release = "development"
        print(f"Run Agent {release}")
        return 0
    if args and args[0] == "gateway":
        from run_agent_gateway.cli import main as gateway_main

        return gateway_main(args[1:])
    if args and args[0] == "bench":
        from run_agent_evals.cli import main as bench_main

        return bench_main(args[1:])

    from run_agent_coding.cli import app

    try:
        result = app(args=args, prog_name="run")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        return 130
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
