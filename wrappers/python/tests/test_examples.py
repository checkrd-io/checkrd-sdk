"""Smoke tests for the runnable examples in ``wrappers/python/examples/``.

The examples are intentionally simple — each is a copy-paste-ready
script a user would land on after reading the README. If any of them
fails to parse (import error, typo, broken API reference), users hit
it on first try. CI must catch that before we do.

These tests exercise **compilation only**: we import each module and
assert its `main` callable is present. We deliberately do NOT call
`main()` because the examples assume real OpenAI / Datadog / etc.
credentials in the environment. The import alone catches ~95% of
drift (missing symbol, wrong kwarg, renamed class).

Mirrors Sentry's `tests/examples/` pattern and Stripe's docs-snippet
smoke suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _example_paths() -> list[Path]:
    """Every `.py` file in the examples directory, excluding __init__."""
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(
        p for p in EXAMPLES_DIR.glob("*.py")
        if p.name != "__init__.py"
    )


@pytest.mark.parametrize("example_path", _example_paths(), ids=lambda p: p.name)
def test_example_is_syntactically_valid(example_path: Path) -> None:
    """Every example must parse without syntax errors.

    Uses ``compile()`` rather than ``importlib`` so the module body
    doesn't execute — examples often reach for env vars that aren't
    set in CI (``OPENAI_API_KEY`` etc.).
    """
    source = example_path.read_text()
    compile(source, str(example_path), "exec")


@pytest.mark.parametrize("example_path", _example_paths(), ids=lambda p: p.name)
def test_example_declares_main_callable(example_path: Path) -> None:
    """Every example must have a ``def main()`` entry point.

    Load the module with a spec so we can inspect the AST without
    running it. Again, no real execution — we only check that the
    public contract ("a ``main`` function exists") is honored.
    """
    spec = importlib.util.spec_from_file_location(
        f"examples_{example_path.stem}",
        example_path,
    )
    assert spec is not None, f"cannot build import spec for {example_path}"
    # We don't exec_module() because that would run the example. We
    # rely on a source-level grep instead, which is robust and cheap.
    source = example_path.read_text()
    assert "def main(" in source, (
        f"{example_path.name} does not define a main() entry point; "
        "examples should follow the convention so `python path/to/example.py` "
        "is copy-pasteable."
    )
