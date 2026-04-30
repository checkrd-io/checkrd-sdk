import { describe, expect, it } from "vitest";

import {
  APIConnectionError,
  APIError,
  APIStatusError,
  APITimeoutError,
  APIUserAbortError,
  AuthenticationError,
  BadRequestError,
  CheckrdError,
  ConflictError,
  InternalServerError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  UnprocessableEntityError,
  makeAPIError,
  type APIStatusErrorDetails,
} from "../src/exceptions.js";

function statusDetails(
  status: number,
  extra: Partial<APIStatusErrorDetails> = {},
): APIStatusErrorDetails & { status: number } {
  return {
    status,
    body: undefined,
    headers: {},
    requestId: undefined,
    message: `HTTP ${String(status)}`,
    ...extra,
  };
}

describe("makeAPIError dispatch table", () => {
  type Ctor = new (details: APIStatusErrorDetails) => APIStatusError;
  const cases: [number, Ctor][] = [
    [400, BadRequestError],
    [401, AuthenticationError],
    [403, PermissionDeniedError],
    [404, NotFoundError],
    [409, ConflictError],
    [422, UnprocessableEntityError],
    [429, RateLimitError],
    [500, InternalServerError],
    [503, InternalServerError],
  ];
  for (const [status, cls] of cases) {
    it(`maps ${status.toString()} → ${cls.name}`, () => {
      const err = makeAPIError(statusDetails(status));
      expect(err).toBeInstanceOf(cls);
      expect(err).toBeInstanceOf(APIStatusError);
      expect(err).toBeInstanceOf(APIError);
      expect(err).toBeInstanceOf(CheckrdError);
      expect((err as APIStatusError).status).toBe(status);
    });
  }

  it("maps null status → APIConnectionError (not APIStatusError)", () => {
    const err = makeAPIError({ status: null, message: "network down" });
    expect(err).toBeInstanceOf(APIConnectionError);
    expect(err).toBeInstanceOf(APIError);
    expect(err).not.toBeInstanceOf(APIStatusError);
  });

  it("falls back to APIStatusError for unrouted 4xx codes", () => {
    const err = makeAPIError(statusDetails(418));
    expect(err).toBeInstanceOf(APIStatusError);
    expect(err.constructor.name).toBe("APIStatusError");
  });

  it("surfaces body.code + body.message on the error", () => {
    const err = makeAPIError(
      statusDetails(400, {
        body: {
          type: "validation",
          code: "missing_field",
          message: "missing agent",
        },
        message: "missing agent",
      }),
    );
    expect(err.code).toBe("missing_field");
    expect(err.message).toBe("missing agent");
  });

  it("preserves the requestId for support correlation", () => {
    const err = makeAPIError(statusDetails(500, { requestId: "req-abc" }));
    expect((err as APIStatusError).requestId).toBe("req-abc");
  });

  it("falls back to http_<status> code when body has no code", () => {
    const err = makeAPIError(statusDetails(429));
    expect(err.code).toBe("http_429");
  });
});

describe("APITimeoutError", () => {
  it("extends APIConnectionError", () => {
    const err = new APITimeoutError();
    expect(err).toBeInstanceOf(APIConnectionError);
    expect(err).toBeInstanceOf(APIError);
    expect(err).toBeInstanceOf(CheckrdError);
    expect(err.code).toBe("api_timeout");
  });
});

describe("APIUserAbortError", () => {
  it("is an APIError (matches OpenAI convention) with stable code", () => {
    const err = new APIUserAbortError();
    expect(err).toBeInstanceOf(APIError);
    expect(err).toBeInstanceOf(CheckrdError);
    expect(err).not.toBeInstanceOf(APIStatusError);
    expect(err).not.toBeInstanceOf(APIConnectionError);
    expect(err.code).toBe("user_abort");
  });
});

describe("APIConnectionError cause chain", () => {
  it("preserves the underlying cause for forensic logging", () => {
    const cause = new Error("ECONNREFUSED");
    const err = new APIConnectionError({ cause });
    expect((err as { cause?: unknown }).cause).toBe(cause);
  });
});

describe("instanceof catch hierarchy (industry standard)", () => {
  it("one CheckrdError catch handles every Checkrd error", () => {
    const errors: Error[] = [
      makeAPIError(statusDetails(429)),
      makeAPIError({ status: null, message: "x" }),
      new APITimeoutError(),
      new APIUserAbortError(),
    ];
    for (const e of errors) {
      let caught = false;
      try {
        throw e;
      } catch (caughtErr) {
        if (caughtErr instanceof CheckrdError) caught = true;
      }
      expect(caught).toBe(true);
    }
  });

  it("APIStatusError catch handles only response-bearing errors", () => {
    const responseBearing = makeAPIError(statusDetails(429));
    expect(responseBearing).toBeInstanceOf(APIStatusError);

    const noResponse = makeAPIError({ status: null, message: "x" });
    expect(noResponse).not.toBeInstanceOf(APIStatusError);

    const timeout = new APITimeoutError();
    expect(timeout).not.toBeInstanceOf(APIStatusError);
  });
});
