"""Allow ``python -m MagentaBench.runner`` to invoke the production runner."""

from .cli import run_main


if __name__ == "__main__":
    raise SystemExit(run_main())

