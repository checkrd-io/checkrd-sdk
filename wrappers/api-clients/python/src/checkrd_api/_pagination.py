"""Pagination iterators.

Mirrors OpenAI / Anthropic shape: list endpoints return a
:class:`SyncPage` (or :class:`AsyncPage` for ``AsyncCheckrd``) that
is *both* a holder for the current page's ``data`` array *and*
iterable across all subsequent pages transparently.

A user can either:

- Iterate (most ergonomic): ``for agent in client.agents.list(): ...``
  — the loop fetches page 2, 3, … as needed.
- Page manually: ``page = client.agents.list(); while page.has_next_page(): page = page.get_next_page()``.

Reference: ``openai-python/src/openai/pagination.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Callable,
    Generic,
    Iterator,
    List,
    Optional,
    TypeVar,
)

if TYPE_CHECKING:
    from ._client import AsyncCheckrd, Checkrd

_T = TypeVar("_T")


@dataclass
class _PageState(Generic[_T]):
    """State kept by both sync and async pages — the page's payload
    plus enough info to fetch the next one if iteration continues."""

    data: List[_T]
    has_more: bool
    next_cursor: Optional[str]
    path: str
    params: dict
    item_decoder: Callable[[dict], _T]


class SyncPage(Generic[_T]):
    """Synchronous cursor-paginated response.

    The class instance returned from a sync ``list()`` call doubles
    as the current page (``page.data``, ``page.has_more``,
    ``page.next_cursor``) and as an iterator that walks every page
    until the cursor exhausts. Most callers just iterate.
    """

    _client: "Checkrd"
    _state: _PageState[_T]

    def __init__(self, client: "Checkrd", state: _PageState[_T]) -> None:
        self._client = client
        self._state = state

    @property
    def data(self) -> List[_T]:
        return self._state.data

    @property
    def has_more(self) -> bool:
        return self._state.has_more

    @property
    def next_cursor(self) -> Optional[str]:
        return self._state.next_cursor

    def has_next_page(self) -> bool:
        return self._state.has_more and self._state.next_cursor is not None

    def get_next_page(self) -> "SyncPage[_T]":
        """Fetch the next page synchronously. Raises :class:`StopIteration`
        when there is no next page."""
        if not self.has_next_page():
            raise StopIteration
        params = dict(self._state.params)
        params["cursor"] = self._state.next_cursor
        return self._client._get_api_list(
            self._state.path,
            params=params,
            item_decoder=self._state.item_decoder,
        )

    def __iter__(self) -> Iterator[_T]:
        page: SyncPage[_T] = self
        while True:
            for item in page.data:
                yield item
            if not page.has_next_page():
                return
            page = page.get_next_page()


class AsyncPage(Generic[_T]):
    """Asynchronous cursor-paginated response. Mirrors :class:`SyncPage`."""

    _client: "AsyncCheckrd"
    _state: _PageState[_T]

    def __init__(self, client: "AsyncCheckrd", state: _PageState[_T]) -> None:
        self._client = client
        self._state = state

    @property
    def data(self) -> List[_T]:
        return self._state.data

    @property
    def has_more(self) -> bool:
        return self._state.has_more

    @property
    def next_cursor(self) -> Optional[str]:
        return self._state.next_cursor

    def has_next_page(self) -> bool:
        return self._state.has_more and self._state.next_cursor is not None

    async def get_next_page(self) -> "AsyncPage[_T]":
        """Fetch the next page. Raises :class:`StopAsyncIteration`
        when there is no next page."""
        if not self.has_next_page():
            raise StopAsyncIteration
        params = dict(self._state.params)
        params["cursor"] = self._state.next_cursor
        return await self._client._get_api_list(
            self._state.path,
            params=params,
            item_decoder=self._state.item_decoder,
        )

    def __aiter__(self) -> AsyncIterator[_T]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[_T]:
        page: AsyncPage[_T] = self
        while True:
            for item in page.data:
                yield item
            if not page.has_next_page():
                return
            page = await page.get_next_page()
