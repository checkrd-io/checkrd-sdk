import { describe, expect, it } from "vitest";

import {
  CursorPage,
  OffsetPage,
  SinglePage,
} from "../src/_pagination.js";

describe("SinglePage", () => {
  it("yields items once and reports no next page", async () => {
    const page = new SinglePage([1, 2, 3]);
    const seen: number[] = [];
    for await (const x of page) seen.push(x);
    expect(seen).toEqual([1, 2, 3]);
    expect(page.hasNextPage()).toBe(false);
    expect(await page.getNextPage()).toBeNull();
  });
});

describe("CursorPage", () => {
  it("auto-fetches subsequent pages on iteration", async () => {
    const pages: Record<string, CursorPage<number>> = {};
    pages["a"] = new CursorPage<number>({
      items: [1, 2],
      nextCursor: "b",
      fetchNext: async (c) => pages[c]!,
    });
    pages["b"] = new CursorPage<number>({
      items: [3, 4],
      nextCursor: "c",
      fetchNext: async (c) => pages[c]!,
    });
    pages["c"] = new CursorPage<number>({
      items: [5],
      nextCursor: null,
    });

    const seen: number[] = [];
    for await (const x of pages["a"]!) seen.push(x);
    expect(seen).toEqual([1, 2, 3, 4, 5]);
  });

  it("terminates cleanly when nextCursor is null", async () => {
    const page = new CursorPage<string>({
      items: ["x"],
      nextCursor: null,
    });
    expect(page.hasNextPage()).toBe(false);
    const seen: string[] = [];
    for await (const x of page) seen.push(x);
    expect(seen).toEqual(["x"]);
  });

  it("terminates when cursor present but no fetchNext callback", async () => {
    // Useful pattern: caller may want single-page iteration with the
    // cursor exposed for manual follow-up later.
    const page = new CursorPage<string>({
      items: ["x"],
      nextCursor: "opaque",
    });
    expect(page.hasNextPage()).toBe(false);
  });
});

describe("OffsetPage", () => {
  it("walks pages until hasMore=false", async () => {
    const pages: OffsetPage<number>[] = [];
    pages.push(
      new OffsetPage<number>({
        items: [1, 2],
        page: 1,
        perPage: 2,
        hasMore: true,
        fetchNext: async (n) => pages[n - 1]!,
      }),
    );
    pages.push(
      new OffsetPage<number>({
        items: [3, 4],
        page: 2,
        perPage: 2,
        hasMore: true,
        fetchNext: async (n) => pages[n - 1]!,
      }),
    );
    pages.push(
      new OffsetPage<number>({
        items: [5],
        page: 3,
        perPage: 2,
        hasMore: false,
      }),
    );

    const seen: number[] = [];
    for await (const x of pages[0]!) seen.push(x);
    expect(seen).toEqual([1, 2, 3, 4, 5]);
  });
});
