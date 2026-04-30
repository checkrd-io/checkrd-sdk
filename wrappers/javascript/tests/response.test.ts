import { describe, expect, it } from "vitest";

import { APIResponse, StreamingAPIResponse } from "../src/_response.js";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json",
      "x-request-id": "req_abc123",
    },
  });
}

function streamingResponse(chunks: string[], status = 200): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const enc = new TextEncoder();
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(stream, {
    status,
    headers: { "content-type": "text/event-stream" },
  });
}

describe("APIResponse — buffered raw response", () => {
  it("exposes status, headers, and request_id from the underlying Response", async () => {
    const r = new APIResponse<{ ok: boolean }>(
      jsonResponse({ ok: true }),
      (bytes) => JSON.parse(new TextDecoder().decode(bytes)) as { ok: boolean },
    );
    expect(r.status).toBe(200);
    expect(r.headers["x-request-id"]).toBe("req_abc123");
    expect(r.requestId).toBe("req_abc123");
  });

  it("caches parse() — second call does not re-parse", async () => {
    let calls = 0;
    const r = new APIResponse<{ n: number }>(
      jsonResponse({ n: 1 }),
      (bytes) => {
        calls += 1;
        return JSON.parse(new TextDecoder().decode(bytes)) as { n: number };
      },
    );
    expect((await r.parse()).n).toBe(1);
    expect((await r.parse()).n).toBe(1);
    expect(calls).toBe(1);
  });
});

describe("StreamingAPIResponse — consumed guard", () => {
  it("starts unconsumed", () => {
    const s = new StreamingAPIResponse(streamingResponse(["a"]));
    expect(s.consumed).toBe(false);
  });

  it("marks itself consumed after the first iterBytes()", async () => {
    const s = new StreamingAPIResponse(streamingResponse(["a", "b"]));
    const out: Uint8Array[] = [];
    for await (const c of s.iterBytes()) out.push(c);
    expect(s.consumed).toBe(true);
    expect(out.length).toBe(2);
  });

  it("throws on a second iteration attempt", async () => {
    const s = new StreamingAPIResponse(streamingResponse(["a"]));
    for await (const _ of s.iterBytes()) {
      // drain
    }
    await expect(async () => {
      for await (const _ of s.iterBytes()) {
        // unreachable
      }
    }).rejects.toThrowError(/can only be consumed once/);
  });

  it("throws on iterText after iterBytes", async () => {
    const s = new StreamingAPIResponse(streamingResponse(["x"]));
    for await (const _ of s.iterBytes()) {
      // drain
    }
    await expect(async () => {
      for await (const _ of s.iterText()) {
        // unreachable
      }
    }).rejects.toThrowError(/can only be consumed once/);
  });
});

describe("StreamingAPIResponse — tee()", () => {
  it("splits into two independently-consumable cursors", async () => {
    const s = new StreamingAPIResponse(streamingResponse(["foo", "bar"]));
    const [a, b] = s.tee();
    expect(s.consumed).toBe(true);

    const dec = new TextDecoder();
    const aChunks: string[] = [];
    for await (const c of a.iterBytes()) aChunks.push(dec.decode(c));
    const bChunks: string[] = [];
    for await (const c of b.iterBytes()) bChunks.push(dec.decode(c));

    expect(aChunks.join("")).toBe("foobar");
    expect(bChunks.join("")).toBe("foobar");
  });

  it("returns two empty streams when the body is null", async () => {
    const r = new Response(null, { status: 204 });
    const s = new StreamingAPIResponse(r);
    const [a, b] = s.tee();
    let count = 0;
    for await (const _ of a.iterBytes()) count += 1;
    for await (const _ of b.iterBytes()) count += 1;
    expect(count).toBe(0);
  });
});

describe("StreamingAPIResponse — toReadableStream()", () => {
  it("returns the underlying body and marks the stream consumed", async () => {
    const s = new StreamingAPIResponse(streamingResponse(["abc"]));
    const rs = s.toReadableStream();
    expect(s.consumed).toBe(true);
    expect(rs).not.toBeNull();
    if (!rs) return;
    const reader = rs.getReader();
    const dec = new TextDecoder();
    let total = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value !== undefined) total += dec.decode(value);
    }
    expect(total).toBe("abc");
  });

  it("returns null for bodyless responses", () => {
    const s = new StreamingAPIResponse(new Response(null, { status: 204 }));
    expect(s.toReadableStream()).toBeNull();
  });
});
