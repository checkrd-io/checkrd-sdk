# Checkrd TypeScript and JavaScript API library

[![NPM version](https://img.shields.io/npm/v/@checkrd/api.svg)](https://npmjs.org/package/@checkrd/api)

This library provides convenient access to the Checkrd Control Plane REST API from server-side TypeScript or JavaScript. The library includes type definitions for all request params and response fields, and offers strong typing, async/await usage, and works on Node.js (18+), Bun, Deno, Cloudflare Workers, and Vercel Edge runtime.

It is generated from the [OpenAPI specification](https://github.com/checkrd/checkrd/blob/main/schemas/api/openapi.json) which itself is derived from the Rust handler signatures in [`crates/api`](https://github.com/checkrd/checkrd/tree/main/crates/api). The hand-written facade layer (`Checkrd` class, resource classes) lives alongside the generated low-level engine.

This library is **separate** from the runtime SDK [`checkrd`](https://npmjs.org/package/checkrd), which lives in the customer's agent process and instruments outbound HTTP. Use `@checkrd/api` for admin scripts, CI tooling, and server-to-server automation.

## Documentation

The REST API documentation can be found at [checkrd.io/docs/api](https://checkrd.io/docs/api). The full TypeScript API reference is at [checkrd.io/docs/javascript](https://checkrd.io/docs/javascript).

## Installation

```sh
npm install @checkrd/api
```

## Usage

```ts
import Checkrd from "@checkrd/api";

const client = new Checkrd({
  apiKey: process.env.CHECKRD_API_KEY,
});

const agent = await client.agents.create({ name: "production-checkout-bot" });
console.log(agent.id, agent.name);
```

While you can provide an `apiKey` keyword argument, we recommend using [`dotenv`](https://www.npmjs.com/package/dotenv) to add `CHECKRD_API_KEY="ck_live_..."` to your `.env` file so your API key isn't stored in source control.

## Pagination

List methods in the Checkrd API are paginated. This library provides auto-paginating async iterators with each list response, so you don't have to request successive pages manually:

```ts
async function fetchAllAgents() {
  const allAgents = [];
  // Automatically fetches more pages as needed.
  for await (const agent of client.agents.list()) {
    allAgents.push(agent);
  }
  return allAgents;
}
```

Alternatively, you can request a single page at a time and use the `.hasNextPage()`, `.getNextPage()`, etc. helpers for more granular control:

```ts
let page = await client.agents.list();
for (const agent of page.data) {
  console.log(agent);
}
while (page.hasNextPage()) {
  page = await page.getNextPage();
  for (const agent of page.data) {
    console.log(agent);
  }
}
```

## Handling errors

When the library is unable to connect to the API, or if the API returns a non-success status code (i.e., 4xx or 5xx response), a subclass of `APIError` will be thrown:

```ts
const agent = await client.agents.create({ name: "" }).catch(async (err) => {
  if (err instanceof Checkrd.APIError) {
    console.log(err.status); // 400
    console.log(err.message); // "Missing required field"
    console.log(err.code); // "missing_required_field"
  } else {
    throw err;
  }
});
```

Error codes are as follows:

| Status Code | Error Type                 |
| ----------- | -------------------------- |
| 400         | `BadRequestError`          |
| 401         | `AuthenticationError`      |
| 403         | `PermissionDeniedError`    |
| 404         | `NotFoundError`            |
| 409         | `ConflictError`            |
| 422         | `UnprocessableEntityError` |
| 429         | `RateLimitError`           |
| >=500       | `InternalServerError`      |
| N/A         | `APIConnectionError`       |

### Retries

Certain errors will be automatically retried 2 times by default, with a short exponential backoff. Connection errors (for example, due to a network connectivity problem), 408 Request Timeout, 409 Conflict, 429 Rate Limit, and >=500 Internal errors will all be retried by default.

You can use the `maxRetries` option to configure or disable this:

```js
// Configure the default for all requests:
const client = new Checkrd({
  maxRetries: 0, // default is 2
});

// Or, configure per-request:
await client.withOptions({ maxRetries: 5 }).agents.list();
```

### Timeouts

By default, requests time out after 60 seconds. You can configure this with a `timeoutMs` option:

```ts
// Configure the default for all requests:
const client = new Checkrd({
  timeoutMs: 20 * 1000, // 20 seconds (default is 60 seconds)
});

// Override per-request:
await client.withOptions({ timeoutMs: 5 * 1000 }).agents.list();
```

On timeout, an `APITimeoutError` is thrown.

Note that requests that time out are [retried twice by default](#retries).

## Edge runtimes

The package runs as-is on Cloudflare Workers, Vercel Edge runtime, and Deno without `node:*` imports. The HTTP transport uses the platform `fetch`; you can override it via the `fetch` option for testing or custom transports:

```ts
import Checkrd from "@checkrd/api";

const client = new Checkrd({
  fetch: customFetch,
});
```

## Versioning

This package generally follows [SemVer](https://semver.org/spec/v2.0.0.html) conventions, though certain backwards-incompatible changes may be released as minor versions:

1. Changes that only affect static types, without breaking runtime behavior.
2. Changes to library internals which are technically public but not intended or documented for external use.
3. Changes that we do not expect to impact the vast majority of users in practice.

We take backwards-compatibility seriously and work hard to ensure you can rely on a smooth upgrade experience.

We are keen for your feedback; please open an [issue](https://github.com/checkrd/checkrd/issues) with questions, bugs, or suggestions.

## Requirements

TypeScript >= 4.9 (recommended).

The following runtimes are supported:

- Node.js 18 LTS or later ([non-EOL](https://endoflife.date/nodejs)).
- Bun 1.0 or later.
- Deno v1.28.0 or later.
- Cloudflare Workers and Vercel Edge runtime (no `node:*` imports).
