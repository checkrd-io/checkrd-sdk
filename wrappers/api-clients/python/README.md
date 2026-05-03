# Checkrd Python API library

[![PyPI version](https://img.shields.io/pypi/v/checkrd-api.svg?label=pypi%20%28stable%29)](https://pypi.org/project/checkrd-api/)

The Checkrd Python library provides convenient access to the Checkrd Control Plane REST API from any Python 3.9+ application. The library includes type definitions for all request params and response fields, and offers both synchronous and asynchronous clients powered by [httpx](https://github.com/encode/httpx).

It is generated from the [OpenAPI specification](https://github.com/checkrd/checkrd/blob/main/schemas/api/openapi.json) which itself is derived from the Rust handler signatures in [`crates/api`](https://github.com/checkrd/checkrd/tree/main/crates/api). The hand-written facade layer (`Checkrd`, `AsyncCheckrd`, resource classes) lives alongside the generated low-level engine.

This library is **separate** from the runtime SDK [`checkrd`](https://pypi.org/project/checkrd/), which lives in the customer's agent process and instruments outbound HTTP. Use `checkrd-api` for admin scripts, CI tooling, and server-to-server automation.

## Documentation

The REST API documentation can be found at [checkrd.io/docs/api](https://checkrd.io/docs/api). The full Python API reference is at [checkrd.io/docs/python](https://checkrd.io/docs/python).

## Installation

```sh
pip install checkrd-api
```

## Usage

The full API of this library can be found in [api.md](api.md).

```python
import os
from checkrd_api import Checkrd

client = Checkrd(
    api_key=os.environ.get("CHECKRD_API_KEY"),  # default
)

agent = client.agents.create(name="production-checkout-bot")
print(agent.id, agent.name)
```

While you can provide an `api_key` keyword argument, we recommend using [python-dotenv](https://pypi.org/project/python-dotenv/) to add `CHECKRD_API_KEY="ck_live_..."` to your `.env` file so your API key isn't stored in source control.

## Async usage

Simply import `AsyncCheckrd` instead of `Checkrd` and use `await` with each API call:

```python
import asyncio
from checkrd_api import AsyncCheckrd


async def main() -> None:
    async with AsyncCheckrd(api_key="ck_live_...") as client:
        agent = await client.agents.create(name="my-agent")
        print(agent.id)


asyncio.run(main())
```

Functionality between the synchronous and asynchronous clients is otherwise identical.

## Pagination

List methods in the Checkrd API are paginated. This library provides auto-paginating iterators with each list response, so you don't have to request successive pages manually:

```python
from checkrd_api import Checkrd

client = Checkrd()

# Automatically fetches more pages as needed.
for agent in client.agents.list():
    print(agent.name)
```

Or, asynchronously:

```python
import asyncio
from checkrd_api import AsyncCheckrd


async def main() -> None:
    client = AsyncCheckrd()
    async for agent in client.agents.list():
        print(agent.name)


asyncio.run(main())
```

Alternatively, you can use the `.has_next_page()`, `.next_cursor`, etc. methods for more granular control working with pages:

```python
first_page = client.agents.list()
if first_page.has_next_page():
    print(f"will fetch next page using these details: {first_page.next_cursor}")
    next_page = first_page.get_next_page()
    print(f"number of items we just fetched: {len(next_page.data)}")

# Remove `await` for non-async usage.
```

## Handling errors

When the library is unable to connect to the API (for example, due to network connection problems or a timeout), a subclass of `checkrd_api.APIConnectionError` is raised.

When the API returns a non-success status code (that is, 4xx or 5xx response), a subclass of `checkrd_api.APIStatusError` is raised, containing `status_code` and `response` properties.

All errors inherit from `checkrd_api.APIError`.

```python
import checkrd_api
from checkrd_api import Checkrd

client = Checkrd()

try:
    client.agents.create(name="")
except checkrd_api.APIConnectionError as e:
    print("The server could not be reached")
    print(e.__cause__)  # an underlying Exception, likely raised within httpx.
except checkrd_api.RateLimitError as e:
    print("A 429 status code was received; we should back off a bit.")
except checkrd_api.APIStatusError as e:
    print("Another non-200-range status code was received")
    print(e.status_code)
    print(e.response)
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

Certain errors are automatically retried 2 times by default, with a short exponential backoff. Connection errors (for example, due to a network connectivity problem), 408 Request Timeout, 409 Conflict, 429 Rate Limit, and >=500 Internal errors are all retried by default.

You can use the `max_retries` option to configure or disable retry settings:

```python
from checkrd_api import Checkrd

# Configure the default for all requests:
client = Checkrd(max_retries=0)  # default is 2

# Or, configure per-request:
client.with_options(max_retries=5).agents.list()
```

### Timeouts

By default, requests time out after 60 seconds. You can configure this with a `timeout` option, which accepts a float (in seconds):

```python
from checkrd_api import Checkrd

# Configure the default for all requests:
client = Checkrd(timeout=20.0)

# More granular control:
client.with_options(timeout=5.0).agents.list()
```

On timeout, an `APITimeoutError` is thrown.

## Versioning

This package generally follows [SemVer](https://semver.org/spec/v2.0.0.html) conventions, though certain backwards-incompatible changes may be released as minor versions:

1. Changes that only affect static types, without breaking runtime behavior.
2. Changes to library internals which are technically public but not intended or documented for external use.
3. Changes that we do not expect to impact the vast majority of users in practice.

We take backwards-compatibility seriously and work hard to ensure you can rely on a smooth upgrade experience.

We are keen for your feedback; please open an [issue](https://github.com/checkrd/checkrd/issues) with questions, bugs, or suggestions.

## Requirements

Python 3.9 or higher.
