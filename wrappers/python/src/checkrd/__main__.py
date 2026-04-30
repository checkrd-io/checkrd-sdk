"""Allow ``python -m checkrd <command>`` invocation.

Equivalent to running the ``checkrd`` console script registered in
``pyproject.toml``. Useful in environments where the script directory
isn't on PATH (containers, air-gapped installs, etc.).
"""

import sys

from checkrd.cli import main

if __name__ == "__main__":
    sys.exit(main())
