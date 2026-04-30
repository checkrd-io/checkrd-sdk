/**
 * Auto-pagination foundation.
 *
 * Mirrors ``openai/src/core/pagination.ts`` shape: every paginated
 * control-plane endpoint returns a {@link Page} instance that is
 * **directly async-iterable** — callers write
 * ``for await (const item of page)`` and pages auto-fetch
 * transparently. No ``getNextPage()`` calls, no manual cursor
 * bookkeeping.
 *
 * The Checkrd control plane uses three pagination shapes across its
 * 14+ list endpoints (orgs, agents, alerts, alert history, audit log,
 * policies, keys, etc.):
 *
 *   - {@link SinglePage} — endpoints not yet paginated, typed as
 *     paginated for forward compatibility.
 *   - {@link CursorPage} — opaque cursor mapped to ``after=<cursor>``
 *     on the next request. The common pattern for time-ordered streams
 *     (audit log, alert history).
 *   - {@link OffsetPage} — ``page=N&per_page=M`` style for older
 *     endpoints.
 *
 * Defining the wrapper types now (no user-facing list endpoints yet)
 * means the FIRST endpoint we ship is paginated from day one — same
 * forward-discipline rationale as ``_response.ts``.
 */

/** Common ancestor for every pagination shape. */
export abstract class BasePage<T> implements AsyncIterable<T> {
  /** Items on the current page. */
  readonly items: readonly T[];

  constructor(items: readonly T[]) {
    this.items = items;
  }

  /** Async iterator — yields all items across pages. */
  async *[Symbol.asyncIterator](): AsyncIterator<T> {
    // Yield the current page's items first, then walk subsequent
    // pages. Avoids aliasing ``this`` to a local variable.
    for (const item of this.items) yield item;
    let page = await this.getNextPage();
    while (page !== null) {
      for (const item of page.items) yield item;
      page = await page.getNextPage();
    }
  }

  /** Whether another page exists. */
  abstract hasNextPage(): boolean;

  /** Fetch the next page; resolves to ``null`` when exhausted. */
  abstract getNextPage(): Promise<BasePage<T> | null>;
}

/**
 * Single-shot page for endpoints that don't paginate yet.
 *
 * Forward-compatibility hook: an endpoint can start as
 * {@link SinglePage} and become {@link CursorPage} later without
 * breaking caller code that uses ``for await``.
 */
export class SinglePage<T> extends BasePage<T> {
  hasNextPage(): boolean {
    return false;
  }

  getNextPage(): Promise<null> {
    return Promise.resolve(null);
  }
}

/** Cursor-based pagination: ``after=<cursor>`` for the next request. */
export class CursorPage<T> extends BasePage<T> {
  readonly nextCursor: string | null;
  private readonly fetchNext:
    | ((cursor: string) => Promise<CursorPage<T>>)
    | null;

  constructor(opts: {
    items: readonly T[];
    nextCursor: string | null;
    fetchNext?: (cursor: string) => Promise<CursorPage<T>>;
  }) {
    super(opts.items);
    this.nextCursor = opts.nextCursor;
    this.fetchNext = opts.fetchNext ?? null;
  }

  hasNextPage(): boolean {
    return this.nextCursor !== null && this.fetchNext !== null;
  }

  async getNextPage(): Promise<CursorPage<T> | null> {
    if (this.nextCursor === null || this.fetchNext === null) return null;
    return this.fetchNext(this.nextCursor);
  }
}

/** Offset-based pagination: ``page=N&per_page=M``. */
export class OffsetPage<T> extends BasePage<T> {
  readonly page: number;
  readonly perPage: number;
  readonly hasMore: boolean;
  private readonly fetchNext:
    | ((page: number) => Promise<OffsetPage<T>>)
    | null;

  constructor(opts: {
    items: readonly T[];
    page: number;
    perPage: number;
    hasMore: boolean;
    fetchNext?: (page: number) => Promise<OffsetPage<T>>;
  }) {
    super(opts.items);
    this.page = opts.page;
    this.perPage = opts.perPage;
    this.hasMore = opts.hasMore;
    this.fetchNext = opts.fetchNext ?? null;
  }

  hasNextPage(): boolean {
    return this.hasMore && this.fetchNext !== null;
  }

  async getNextPage(): Promise<OffsetPage<T> | null> {
    if (!this.hasMore || this.fetchNext === null) return null;
    return this.fetchNext(this.page + 1);
  }
}
