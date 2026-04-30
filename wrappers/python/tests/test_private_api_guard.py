"""Tests for the PEP 562 ``__getattr__`` guard on ``checkrd``.

Pins the contract that defines the public SDK surface:

- Every name in ``__all__`` is reachable via attribute access.
- *New* underscore-prefixed names that aren't in ``__dict__`` raise
  ``AttributeError`` with a helpful, actionable message — both via
  ``checkrd._foo`` and ``from checkrd import _foo``.
- Documented limitations: pre-existing leaks already in ``__dict__``
  bypass the hook (PEP 562 only fires on misses), and submodule
  imports (``from checkrd._state import _GlobalContext``) use
  ``importlib`` directly, not the parent package's ``__getattr__``.
- ``dir(checkrd)`` returns only the public ``__all__`` names so REPL
  autocompletion does not tempt callers into private territory.
"""

from __future__ import annotations

import pytest


class TestPublicSurface:
    """Every documented entry point resolves and isn't accidentally
    redacted by the guard."""

    def test_all_names_are_reachable_via_attribute_access(self) -> None:
        import checkrd

        for name in checkrd.__all__:
            assert hasattr(checkrd, name), (
                f"{name!r} is in checkrd.__all__ but not reachable via "
                f"attribute access — this would break documented usage"
            )

    def test_dir_matches_all(self) -> None:
        import checkrd

        # ``dir()`` must equal ``sorted(__all__)`` so help / REPL
        # autocomplete shows only the public surface.
        assert dir(checkrd) == sorted(checkrd.__all__)


class TestPrivateGuardBlocksNewNames:
    """The forward-looking guard fires on any underscore-prefixed
    name that wasn't pre-imported into the package namespace."""

    def test_attribute_access_to_new_private_name_raises(self) -> None:
        import checkrd

        with pytest.raises(AttributeError) as exc:
            checkrd._this_name_does_not_exist  # noqa: B018  - deliberate
        # The message must mention the name AND nudge the user toward
        # the documented surface.
        msg = str(exc.value)
        assert "_this_name_does_not_exist" in msg
        assert "private" in msg
        assert "__all__" in msg

    def test_from_import_of_new_private_name_raises(self) -> None:
        # ``from checkrd import _foo`` resolves through the same PEP
        # 562 hook because ``_foo`` is not in ``__dict__``.
        with pytest.raises(ImportError):
            exec("from checkrd import _absolutely_no_such_attribute")

    def test_unknown_public_name_gets_helpful_error(self) -> None:
        # Non-underscore unknown names also get a redirect to the
        # public surface — useful when a typo happens.
        import checkrd

        with pytest.raises(AttributeError) as exc:
            checkrd.PolicyEngie  # noqa: B018  - deliberate typo
        assert "PolicyEngie" in str(exc.value)
        assert "__all__" in str(exc.value)

    def test_dunder_misses_use_default_path(self) -> None:
        # ``__getattr__`` should only catch normal names. Dunder
        # lookups (e.g., during pickling or copy.deepcopy) need the
        # default behaviour or third-party machinery breaks.
        import checkrd

        with pytest.raises(AttributeError) as exc:
            checkrd.__no_such_dunder__  # noqa: B018
        # Default path message is what we expect — not the "private
        # name" message reserved for ``_foo``.
        assert "private" not in str(exc.value)


class TestDocumentedLimitations:
    """Two limitations are documented in the guard's docstring; both
    are pinned here so future contributors can't quietly tighten the
    contract without updating the docs."""

    def test_pre_existing_leaks_bypass_the_hook(self) -> None:
        # ``_no_throw`` is an internal helper that the package
        # re-exports for cross-module use. PEP 562 only fires on
        # misses, so this name is reachable. It is NOT public —
        # documented as such in the guard's docstring.
        import checkrd

        # Just verify the leak exists; we don't pin its shape because
        # any change would belong to whoever removes the leak in a
        # follow-up.
        assert hasattr(checkrd, "_no_throw")

    def test_submodule_imports_still_work(self) -> None:
        # The guard sits on the *package*. Submodule imports go
        # through ``importlib`` and never touch the parent's
        # ``__getattr__``.
        from checkrd._state import _GlobalContext  # type: ignore[import-not-found]

        assert _GlobalContext is not None
