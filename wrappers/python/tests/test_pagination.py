"""Tests for the auto-pagination foundation in :mod:`checkrd._pagination`.

Verifies the iteration contract — callers should never need to drive
``get_next_page()`` manually, and iteration must walk transparently
across all pages until the cursor / has_more sentinel signals exhaustion.
"""

from __future__ import annotations

import pytest

from checkrd._pagination import (
    AsyncCursorPage,
    AsyncOffsetPage,
    AsyncSinglePage,
    CursorPage,
    OffsetPage,
    SinglePage,
)


def test_single_page_iterates_once() -> None:
    page = SinglePage([1, 2, 3])
    assert list(page) == [1, 2, 3]
    assert page.has_next_page() is False
    assert page.get_next_page() is None


def test_cursor_page_walks_multiple_pages() -> None:
    """``for item in page`` must auto-fetch subsequent pages."""
    pages = {
        "a": CursorPage([1, 2], next_cursor="b", fetch_next=lambda c: pages[c]),
        "b": CursorPage([3, 4], next_cursor="c", fetch_next=lambda c: pages[c]),
        "c": CursorPage([5], next_cursor=None),
    }
    assert list(pages["a"]) == [1, 2, 3, 4, 5]


def test_cursor_page_terminates_when_cursor_is_none() -> None:
    page = CursorPage(["x"], next_cursor=None)
    assert list(page) == ["x"]
    assert page.has_next_page() is False


def test_cursor_page_terminates_when_no_fetch_callback() -> None:
    """Cursor present but no fetch_next means the caller wants
    items-only iteration on a single page."""
    page = CursorPage(["x"], next_cursor="opaque", fetch_next=None)
    assert list(page) == ["x"]
    assert page.has_next_page() is False


def test_offset_page_walks_pages() -> None:
    pages = [
        OffsetPage(
            [1, 2], page=1, per_page=2, has_more=True,
            fetch_next=lambda n: pages[n - 1],
        ),
        OffsetPage(
            [3, 4], page=2, per_page=2, has_more=True,
            fetch_next=lambda n: pages[n - 1],
        ),
        OffsetPage(
            [5], page=3, per_page=2, has_more=False,
        ),
    ]
    assert list(pages[0]) == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_async_single_page_iterates() -> None:
    page: AsyncSinglePage[int] = AsyncSinglePage([10, 20, 30])
    seen: list[int] = []
    async for item in page:
        seen.append(item)
    assert seen == [10, 20, 30]


@pytest.mark.asyncio
async def test_async_cursor_page_walks() -> None:
    """Async equivalent of cursor pagination — uses ``async for``."""
    pages: dict[str, AsyncCursorPage[int]] = {}

    async def _fetch(cursor: str) -> AsyncCursorPage[int]:
        return pages[cursor]

    pages["a"] = AsyncCursorPage([1, 2], next_cursor="b", fetch_next=_fetch)
    pages["b"] = AsyncCursorPage([3], next_cursor=None)

    seen: list[int] = []
    async for item in pages["a"]:
        seen.append(item)
    assert seen == [1, 2, 3]


@pytest.mark.asyncio
async def test_async_offset_page_walks() -> None:
    pages: list[AsyncOffsetPage[int]] = []

    async def _fetch(n: int) -> AsyncOffsetPage[int]:
        return pages[n - 1]

    pages.append(
        AsyncOffsetPage(
            [1, 2], page=1, per_page=2, has_more=True, fetch_next=_fetch,
        ),
    )
    pages.append(
        AsyncOffsetPage(
            [3], page=2, per_page=2, has_more=False,
        ),
    )

    seen: list[int] = []
    async for item in pages[0]:
        seen.append(item)
    assert seen == [1, 2, 3]
