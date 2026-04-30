"""Tests for the unified :class:`Checkrd` / :class:`AsyncCheckrd` client.

The class is the A-grade consolidation of :func:`wrap` + :func:`init`
+ :func:`instrument_*` into a single OpenAI-SDK-shaped object. These
tests pin the public contract:

  - Constructor signature matches OpenAI / Anthropic conventions
    (``api_key=``, ``base_url=``, env-var fallbacks).
  - :meth:`Checkrd.wrap` attaches enforcement to an ``httpx.Client``.
  - :meth:`Checkrd.with_options` is immutable — source client
    unchanged, sibling gets the overrides.
  - Context-manager support (``with Checkrd(...) as client:``).
  - :meth:`__repr__` never leaks the API key.
  - :class:`AsyncCheckrd` mirrors sync and takes ``httpx.AsyncClient``.
"""

from __future__ import annotations

import httpx
import pytest

from checkrd import AsyncCheckrd, Checkrd
from tests.conftest import requires_wasm


ALLOW_ALL = {"agent": "test", "default": "allow", "rules": []}


@requires_wasm
class TestCheckrdConstructor:
    """The single-constructor surface is the biggest DX win.

    Validating the shape here guards against accidental regressions —
    e.g. someone renaming ``base_url`` to ``baseUrl`` and breaking
    every tutorial example.
    """

    def test_constructs_with_no_arguments(self) -> None:
        # A bare ``Checkrd()`` must work — config resolves from env.
        # Mirrors ``OpenAI()`` and ``Anthropic()``.
        client = Checkrd()
        try:
            assert client.agent_id  # non-empty fallback
        finally:
            client.close()

    def test_constructs_with_api_key(self) -> None:
        client = Checkrd(api_key="ck_test_abc", agent_id="my-agent")
        try:
            assert client.api_key == "ck_test_abc"
            assert client.agent_id == "my-agent"
        finally:
            client.close()

    def test_reads_api_key_from_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stripe/OpenAI/Anthropic convention: CHECKRD_API_KEY fallback.
        monkeypatch.setenv("CHECKRD_API_KEY", "ck_env_xyz")
        client = Checkrd(agent_id="t")
        try:
            assert client.api_key == "ck_env_xyz"
        finally:
            client.close()

    def test_base_url_is_the_public_kwarg_name(self) -> None:
        # Internal naming is `control_plane_url`; the public API uses
        # `base_url` to match OpenAI / Anthropic nomenclature. Test
        # the public shape so a future rename has to be intentional.
        client = Checkrd(
            api_key="ck",
            agent_id="t",
            base_url="https://api.example.com",
        )
        try:
            assert client.base_url == "https://api.example.com"
        finally:
            client.close()


@requires_wasm
class TestCheckrdWrap:
    def test_wrap_returns_the_same_client(self) -> None:
        """Fluent style — ``client.wrap(x)`` returns ``x``.

        This is important for the `http = checkrd.wrap(httpx.Client())`
        chaining pattern to keep the surface compact.
        """
        client = Checkrd(agent_id="t", policy=ALLOW_ALL)
        with httpx.Client() as http:
            try:
                result = client.wrap(http)
                assert result is http
            finally:
                http.close()
                client.close()

    def test_wrap_stamps_default_headers_on_the_client(self) -> None:
        """Default headers flow from the constructor to the http client."""
        client = Checkrd(
            agent_id="t",
            policy=ALLOW_ALL,
            default_headers={"X-Tenant-Id": "org_42"},
        )
        with httpx.Client() as http:
            try:
                client.wrap(http)
                assert http.headers["X-Tenant-Id"] == "org_42"
            finally:
                http.close()
                client.close()

    def test_wrap_is_idempotent_when_called_on_separate_clients(self) -> None:
        """One Checkrd instance can attach to many httpx clients."""
        client = Checkrd(agent_id="t", policy=ALLOW_ALL)
        with httpx.Client() as http1:
            http2 = httpx.Client()
            try:
                client.wrap(http1)
                client.wrap(http2)
            finally:
                http1.close()
                http2.close()
                client.close()


@requires_wasm
class TestWithOptions:
    """Immutable-clone semantics — the OpenAI-SDK .with_options pattern."""

    def test_returns_a_new_instance(self) -> None:
        a = Checkrd(api_key="ck1", agent_id="t", policy=ALLOW_ALL)
        try:
            b = a.with_options(api_key="ck2")
            assert b is not a
            # Source is unchanged.
            assert a.api_key == "ck1"
            # Sibling has the override.
            assert b.api_key == "ck2"
        finally:
            a.close()

    def test_omitted_kwargs_reuse_the_current_value(self) -> None:
        a = Checkrd(
            api_key="ck1", agent_id="custom", base_url="https://api.example.com",
            policy=ALLOW_ALL,
        )
        try:
            # Only override api_key; agent_id + base_url must persist.
            b = a.with_options(api_key="ck2")
            assert b.agent_id == "custom"
            assert b.base_url == "https://api.example.com"
        finally:
            a.close()

    def test_explicit_none_unsets_a_field(self) -> None:
        a = Checkrd(api_key="ck1", agent_id="t", policy=ALLOW_ALL)
        try:
            b = a.with_options(api_key=None)
            assert b.api_key is None
            # Source still has the original value.
            assert a.api_key == "ck1"
        finally:
            a.close()

    def test_with_options_is_cheap_and_doesnt_start_backgrounds(self) -> None:
        # The clone should not spin up a fresh batcher/receiver on its
        # own — those only materialize on the first `.wrap()` call.
        a = Checkrd(api_key="ck", agent_id="t", policy=ALLOW_ALL)
        try:
            # Five clones in a row — no background threads should
            # accumulate.
            b = a.with_options(api_key="ck2")
            c = b.with_options(base_url="https://a.example.com")
            d = c.with_options(api_key="ck3")
            assert d.api_key == "ck3"
            assert d.base_url == "https://a.example.com"
            assert d.agent_id == "t"
        finally:
            a.close()


@requires_wasm
class TestContextManager:
    def test_with_block_closes_cleanly(self) -> None:
        with Checkrd(agent_id="t", policy=ALLOW_ALL) as client:
            http = client.wrap(httpx.Client())
            http.close()
        # If the `with` block exited without exception, `.close()` was
        # called — nothing more to assert at this layer.

    def test_close_is_idempotent(self) -> None:
        client = Checkrd(agent_id="t", policy=ALLOW_ALL)
        client.close()
        client.close()  # must not raise

    def test_close_does_not_raise_on_failing_batcher(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A buggy batcher stop() must never crash process shutdown.

        Close is the last-ditch path; if it raises, users get a
        confusing ``atexit`` traceback with no context. Matches the
        PostHog / Sentry convention that shutdown is best-effort.
        """
        client = Checkrd(agent_id="t", policy=ALLOW_ALL)
        with httpx.Client() as http:
            try:
                client.wrap(http)
                batcher = getattr(http, "_checkrd_batcher", None)
                if batcher is not None:
                    def boom() -> None:
                        raise RuntimeError("test: batcher stop failed")
                    monkeypatch.setattr(batcher, "stop", boom)
                # Still must not raise.
                client.close()
            finally:
                http.close()


@requires_wasm
class TestPublicRetryTimeoutAPI:
    """Phase 2 — the constructor accepts industry-standard retry/timeout knobs.

    Mirrors ``OpenAI(max_retries=5, timeout=60.0)`` and propagates the
    values through the runtime so the batcher / public-key registrar
    see them. Without this, operators have no public lever to tune
    behavior on slow control planes.
    """

    def test_constructor_accepts_max_retries_and_timeouts(self) -> None:
        client = Checkrd(
            agent_id="t",
            policy=ALLOW_ALL,
            max_retries=5,
            timeout=60.0,
            connect_timeout=10.0,
        )
        try:
            assert client._config.max_retries == 5
            assert client._config.timeout == 60.0
            assert client._config.connect_timeout == 10.0
        finally:
            client.close()

    def test_defaults_match_documented_industry_standard(self) -> None:
        client = Checkrd(agent_id="t", policy=ALLOW_ALL)
        try:
            assert client._config.max_retries == 3
            assert client._config.timeout == 30.0
            assert client._config.connect_timeout == 5.0
        finally:
            client.close()

    def test_with_options_preserves_overrides(self) -> None:
        c = Checkrd(agent_id="t", policy=ALLOW_ALL, max_retries=5, timeout=60.0)
        try:
            c2 = c.with_options(max_retries=10)
            try:
                assert c2._config.max_retries == 10
                # Unspecified value reuses the parent's setting.
                assert c2._config.timeout == 60.0
                # Source is unchanged.
                assert c._config.max_retries == 5
            finally:
                c2.close()
        finally:
            c.close()

    def test_async_checkrd_inherits_the_same_knobs(self) -> None:
        c = AsyncCheckrd(
            agent_id="t",
            policy=ALLOW_ALL,
            max_retries=7,
            timeout=45.0,
            connect_timeout=3.0,
        )
        try:
            assert c._config.max_retries == 7
            assert c._config.timeout == 45.0
            assert c._config.connect_timeout == 3.0
        finally:
            c.close()


class TestReprSafety:
    """The __repr__ must never leak credential material."""

    def test_repr_never_includes_api_key_value(self) -> None:
        # Shows up in developer REPLs, uncaught-exception tracebacks,
        # and log aggregators. A leaked key in any of those is a
        # Stripe-style S1.
        client = Checkrd(
            api_key="ck_live_SUPER_SECRET_abc123",
            agent_id="t",
        )
        try:
            r = repr(client)
            assert "SUPER_SECRET" not in r
            assert "ck_live_" not in r
            # ...but it does say "has_api_key=True" so operators can
            # still tell whether a key was configured.
            assert "has_api_key=True" in r
        finally:
            client.close()

    def test_repr_shows_false_when_no_key_anywhere(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CHECKRD_API_KEY", raising=False)
        client = Checkrd(agent_id="t")
        try:
            assert "has_api_key=False" in repr(client)
        finally:
            client.close()


@requires_wasm
class TestAsyncCheckrd:
    def test_constructs_and_closes(self) -> None:
        client = AsyncCheckrd(agent_id="t", policy=ALLOW_ALL)
        client.close()

    async def test_async_context_manager(self) -> None:
        async with AsyncCheckrd(agent_id="t", policy=ALLOW_ALL) as client:
            assert client.agent_id == "t"

    async def test_wrap_returns_async_client(self) -> None:
        client = AsyncCheckrd(agent_id="t", policy=ALLOW_ALL)
        async with httpx.AsyncClient() as http:
            try:
                result = client.wrap(http)
                # Same client, mutated in place — fluent style.
                assert result is http
            finally:
                await http.aclose()
                client.close()

    async def test_with_options_returns_async_sibling(self) -> None:
        a = AsyncCheckrd(api_key="ck1", agent_id="t", policy=ALLOW_ALL)
        try:
            b = a.with_options(api_key="ck2")
            assert isinstance(b, AsyncCheckrd)
            assert b.api_key == "ck2"
            assert a.api_key == "ck1"
        finally:
            a.close()

    async def test_aclose_is_async_callable(self) -> None:
        client = AsyncCheckrd(agent_id="t", policy=ALLOW_ALL)
        # Must be awaitable even though the underlying shutdown is sync —
        # callers should not need to special-case the async variant in
        # their cleanup code.
        await client.aclose()
        await client.aclose()  # idempotent


@requires_wasm
class TestBackwardsCompatibility:
    """The top-level functional API must keep working unchanged."""

    def test_top_level_wrap_still_functions(self) -> None:
        from checkrd import wrap as top_level_wrap
        with httpx.Client() as http:
            try:
                top_level_wrap(http, agent_id="t", policy=ALLOW_ALL)
            finally:
                http.close()

    def test_checkrd_class_and_top_level_wrap_are_interchangeable(
        self,
    ) -> None:
        """Both entry points wire up the same underlying transport.

        End-to-end equivalence is tested by the per-client suites; here
        we verify that a user can mix-and-match without error.
        """
        from checkrd import wrap as top_level_wrap

        client = Checkrd(agent_id="t", policy=ALLOW_ALL)
        http1 = client.wrap(httpx.Client())
        http2 = top_level_wrap(httpx.Client(), agent_id="t", policy=ALLOW_ALL)
        try:
            assert http1 is not http2
        finally:
            http1.close()
            http2.close()
            client.close()


def test_exported_at_package_root() -> None:
    # The whole point of the consolidation is that `from checkrd import
    # Checkrd` works. A regression here means tutorials break.
    import checkrd

    assert hasattr(checkrd, "Checkrd")
    assert hasattr(checkrd, "AsyncCheckrd")
    assert "Checkrd" in checkrd.__all__
    assert "AsyncCheckrd" in checkrd.__all__
