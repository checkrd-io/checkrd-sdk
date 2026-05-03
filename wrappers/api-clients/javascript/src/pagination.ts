/**
 * Pagination iterator.
 *
 * Mirrors the OpenAI / Anthropic / Stainless TS shape: list
 * methods return a ``PagePromise`` that is awaitable to get the
 * first page, async-iterable to walk every page, and exposes
 * ``hasNextPage()`` / ``getNextPage()`` for manual cursor walking.
 *
 * Usage::
 *
 *     for await (const agent of client.agents.list()) {
 *       console.log(agent.name);  // fetches page 2, 3, … on demand
 *     }
 *
 *     // Or: get just the first page synchronously after await.
 *     const page = await client.agents.list();
 *     for (const agent of page.data) console.log(agent.name);
 *     if (page.hasNextPage()) {
 *       const next = await page.getNextPage();
 *     }
 */

import type { Checkrd } from "./client.js";

/** Wire shape of every paginated response body. */
export interface PaginatedBody<T> {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface PageOptions {
  path: string;
  params: Record<string, unknown>;
  decode: (raw: unknown) => unknown;
}

/**
 * One page of results plus a cursor to the next. Both an array
 * holder (``page.data``) and async-iterable across all pages.
 */
export class Page<T> implements AsyncIterable<T> {
  readonly data: T[];
  readonly hasMore: boolean;
  readonly nextCursor: string | null;
  private readonly client: Checkrd;
  private readonly opts: PageOptions;

  constructor(
    client: Checkrd,
    body: PaginatedBody<T>,
    opts: PageOptions,
  ) {
    this.client = client;
    this.opts = opts;
    this.data = body.data;
    this.hasMore = body.has_more;
    this.nextCursor = body.next_cursor;
  }

  hasNextPage(): boolean {
    return this.hasMore && this.nextCursor !== null;
  }

  async getNextPage(): Promise<Page<T>> {
    if (!this.hasNextPage()) {
      throw new Error("getNextPage() called when no next page exists");
    }
    return this.client._getApiList<T>(this.opts.path, {
      ...this.opts.params,
      cursor: this.nextCursor,
    });
  }

  async *[Symbol.asyncIterator](): AsyncIterator<T> {
    let page: Page<T> = this;
    while (true) {
      for (const item of page.data) yield item;
      if (!page.hasNextPage()) return;
      page = await page.getNextPage();
    }
  }
}

/**
 * The promise type returned by every ``list()`` method. Awaitable
 * to get the first :class:`Page`; async-iterable to walk every
 * page transparently.
 *
 * Mirrors OpenAI's ``PagePromise`` — same dual await/iterate
 * affordance.
 */
export class PagePromise<T> implements PromiseLike<Page<T>>, AsyncIterable<T> {
  private readonly inner: Promise<Page<T>>;

  constructor(inner: Promise<Page<T>>) {
    this.inner = inner;
  }

  then<TResult1 = Page<T>, TResult2 = never>(
    onfulfilled?: ((value: Page<T>) => TResult1 | PromiseLike<TResult1>) | undefined | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | undefined | null,
  ): PromiseLike<TResult1 | TResult2> {
    return this.inner.then(onfulfilled, onrejected);
  }

  catch<TResult = never>(
    onrejected?: ((reason: unknown) => TResult | PromiseLike<TResult>) | undefined | null,
  ): Promise<Page<T> | TResult> {
    return this.inner.catch(onrejected);
  }

  finally(onfinally?: (() => void) | undefined | null): Promise<Page<T>> {
    return this.inner.finally(onfinally);
  }

  async *[Symbol.asyncIterator](): AsyncIterator<T> {
    const page = await this.inner;
    yield* page;
  }
}
