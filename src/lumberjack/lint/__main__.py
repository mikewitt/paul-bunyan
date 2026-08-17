"""Entry shim: `python -m lumberjack.lint` runs the CLI in `cli.py`.

Nothing lives here on purpose — see `cli.py`'s docstring for why the real
CLI cannot sit in this file.
"""

from lumberjack.lint.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
