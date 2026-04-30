"""Auto-pagination foundation.

Mirrors ``openai/pagination.py`` shape: every paginated control-plane
endpoint returns a :class:`Page` (or :class:`AsyncPage`) instance that
is **directly iterable** — callers write ``for item in page:`` and
pages auto-fetch transparently. No ``next_page()`` calls, no manual
cursor bookkeeping.

Three concrete pagination shapes match what the Checkrd control plane
uses on its 14+ list endpoints (orgs, agents, alerts, alert history,
audit log, policies, keys, etc.):

  - :class:`SinglePage` — endpoints not yet paginated, but typed as
    paginated for forward compatibility. Equivalent to OpenAI's
    ``SyncPage``.
  - :class:`CursorPage` — opaque cursor that maps to ``after=<cursor>``
    on the next request. Equivalent to OpenAI's ``SyncCursorPage`` —
    the more common pattern for time-ordered streams (audit log,
    alert history).
  - :class:`OffsetPage` — ``page=N&per_page=M`` style. Used by older
    list endpoints that still rely on offset pagination.

Both sync (:class:`Page`, :class:`CursorPage`, :class:`OffsetPage`)
and async (:class:`AsyncPage`, :class:`AsyncCursorPage`,
:class:`AsyncOffsetPage`) variants share a common iteration contract
so type-checkers can statically verify ``for await item in page`` for
the async case and ``for item in page`` for the sync case.

Usage shape (planned for the first list endpoint we ship)::

    # Sync — automatic next-page fetch on iteration boundary.
    for org in client.orgs.list():
        ...

    # Async — same, with `await` in the for header.
    async for org in client.orgs.list():
        ...

    # Manual page boundary, when the caller needs the cursor:
    page = client.orgs.list()
    while page is not None:
        for org in page.items:
            ...
        page = page.get_next_page()

This module currently defines the wrapper types only; no user-facing
list endpoints exist yet. Defining them now means the FIRST endpoint
ships paginated from day one — the same forward-discipline rationale
as :mod:`checkrd._response`.
"""

from __future__ import annotations

from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generic,
    Iterator,
    List,
    Optional,
    TypeVar,
)


T = TypeVar("T")


# ===========================================================================
# Sync
# ===========================================================================


class BasePage(Generic[T]):
    """Common ancestor for every sync page type. Iteration is
    auto-paginating: ``for item in page`` walks through the current
    page's items and then transparently fetches subsequent pages.
    """

    items: List[T]

    def __init__(self, items: List[T]) -> None:
        self.items = items

    def __iter__(self) -> Iterator[T]:
        page: Optional["BasePage[T]"] = self
        while page is not None:
            yield from page.items
            page = page.get_next_page()

    def has_next_page(self) -> bool:
        """Concrete subclasses override to advertise pagination state."""
        return False

    def get_next_page(self) -> Optional["BasePage[T]"]:
        """Concrete subclasses fetch the next page."""
        return None


class SinglePage(BasePage[T]):
    """Single-shot page for endpoints that don't paginate yet.

    Forward-compatibility hook: an endpoint can start as ``SinglePage``
    and become ``CursorPage`` later without breaking caller code that
    iterates with ``for item in page``.
    """


class CursorPage(BasePage[T]):
    """Cursor-based pagination: ``after=<cursor>`` for the next request.

    The :paramref:`fetch_next` callable encapsulates the HTTP request
    construction so the page itself stays decoupled from the API
    client. Following the OpenAI / Stripe convention, the cursor is
    derived from the last item's ``.id`` if the resource has one.
    """

    next_cursor: Optional[str]

    def __init__(
        self,
        items: List[T],
        *,
        next_cursor: Optional[str],
        fetch_next: Optional[Callable[[str], "CursorPage[T]"]] = None,
    ) -> None:
        super().__init__(items)
        self.next_cursor = next_cursor
        self._fetch_next = fetch_next

    def has_next_page(self) -> bool:
        return self.next_cursor is not None and self._fetch_next is not None

    def get_next_page(self) -> Optional["CursorPage[T]"]:
        if not self.has_next_page() or self._fetch_next is None or self.next_cursor is None:
            return None
        return self._fetch_next(self.next_cursor)


class OffsetPage(BasePage[T]):
    """Offset-based pagination: ``page=N&per_page=M``.

    Less robust than cursor pagination (writes during iteration can
    skip or duplicate items) but supported for endpoints that haven't
    migrated. Returns a :class:`SinglePage`-equivalent iteration
    contract from the caller's perspective.
    """

    page: int
    per_page: int
    has_more: bool

    def __init__(
        self,
        items: List[T],
        *,
        page: int,
        per_page: int,
        has_more: bool,
        fetch_next: Optional[Callable[[int], "OffsetPage[T]"]] = None,
    ) -> None:
        super().__init__(items)
        self.page = page
        self.per_page = per_page
        self.has_more = has_more
        self._fetch_next = fetch_next

    def has_next_page(self) -> bool:
        return self.has_more and self._fetch_next is not None

    def get_next_page(self) -> Optional["OffsetPage[T]"]:
        if not self.has_next_page() or self._fetch_next is None:
            return None
        return self._fetch_next(self.page + 1)


# ===========================================================================
# Async
# ===========================================================================


class BaseAsyncPage(Generic[T]):
    """Async pagination contract — ``async for item in page``."""

    items: List[T]

    def __init__(self, items: List[T]) -> None:
        self.items = items

    def __aiter__(self) -> AsyncIterator[T]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[T]:
        page: Optional["BaseAsyncPage[T]"] = self
        while page is not None:
            for item in page.items:
                yield item
            page = await page.get_next_page()

    async def has_next_page(self) -> bool:
        return False

    async def get_next_page(self) -> Optional["BaseAsyncPage[T]"]:
        return None


class AsyncSinglePage(BaseAsyncPage[T]):
    """Async equivalent of :class:`SinglePage`."""


class AsyncCursorPage(BaseAsyncPage[T]):
    """Async equivalent of :class:`CursorPage`."""

    next_cursor: Optional[str]

    def __init__(
        self,
        items: List[T],
        *,
        next_cursor: Optional[str],
        fetch_next: Optional[Callable[[str], Awaitable["AsyncCursorPage[T]"]]] = None,
    ) -> None:
        super().__init__(items)
        self.next_cursor = next_cursor
        self._fetch_next = fetch_next

    async def has_next_page(self) -> bool:
        return self.next_cursor is not None and self._fetch_next is not None

    async def get_next_page(self) -> Optional["AsyncCursorPage[T]"]:
        if (
            not await self.has_next_page()
            or self._fetch_next is None
            or self.next_cursor is None
        ):
            return None
        return await self._fetch_next(self.next_cursor)


class AsyncOffsetPage(BaseAsyncPage[T]):
    """Async equivalent of :class:`OffsetPage`."""

    page: int
    per_page: int
    has_more: bool

    def __init__(
        self,
        items: List[T],
        *,
        page: int,
        per_page: int,
        has_more: bool,
        fetch_next: Optional[Callable[[int], Awaitable["AsyncOffsetPage[T]"]]] = None,
    ) -> None:
        super().__init__(items)
        self.page = page
        self.per_page = per_page
        self.has_more = has_more
        self._fetch_next = fetch_next

    async def has_next_page(self) -> bool:
        return self.has_more and self._fetch_next is not None

    async def get_next_page(self) -> Optional["AsyncOffsetPage[T]"]:
        if not await self.has_next_page() or self._fetch_next is None:
            return None
        return await self._fetch_next(self.page + 1)


__all__ = [
    "BasePage",
    "SinglePage",
    "CursorPage",
    "OffsetPage",
    "BaseAsyncPage",
    "AsyncSinglePage",
    "AsyncCursorPage",
    "AsyncOffsetPage",
]


# Provide ``__module__`` aliases used in stack traces / debuggers so
# the pagination types appear under ``checkrd._pagination`` regardless
# of how they were imported. Avoids confusion when users grep through
# logs for pagination-related stacks.
for _cls in (
    BasePage, SinglePage, CursorPage, OffsetPage,
    BaseAsyncPage, AsyncSinglePage, AsyncCursorPage, AsyncOffsetPage,
):
    _cls.__module__ = "checkrd._pagination"
del _cls
