## Setting up the environment

```sh
# From the repo root:
python -m venv .venv
source .venv/bin/activate
pip install -e 'wrappers/api-clients/python[dev]'
```

## Modifying/Adding code

The `_generated/` subpackage is materialized from `schemas/api/openapi.json` by the
[openapi-python-client](https://github.com/openapi-generators/openapi-python-client) generator.
**Do not edit anything under `_generated/` by hand** — your changes will be overwritten the next
time the generator runs.

The hand-written facade layer (`__init__.py`, `_client.py`, `_exceptions.py`,
`_pagination.py`, `_resources/*.py`, `types.py`) is the source of truth for the
public surface. When the API gets a new resource:

1. Add `#[utoipa::path]` annotations to the new handlers in `crates/api/src/routes/<thing>.rs`.
2. Run `make openapi` to regenerate the spec.
3. Run `make api-clients-python` to regenerate `_generated/`.
4. Add a `_resources/<thing>.py` mirroring the `_resources/agents.py` pattern.
5. Wire it as a `cached_property` on `Checkrd` and `AsyncCheckrd` in `_client.py`.
6. Re-export models from `types.py`.

CI runs `make openapi-check` and fails on any drift between `crates/api` and the
committed spec — same drift guard as the dashboard's `typeshare`-generated TS.

## Adding and running examples

All files in the [examples](examples/) folder are not modified by the generator and can be freely
edited or added to.

```py
# add an example to examples/<your-example>.py

#!/usr/bin/env -S uv run --script

#

# /// script

# dependencies = [

#     "checkrd-api",

# ]

# ///

# ...
```

```sh
chmod +x examples/<your-example>.py
# run the example against your api
./examples/<your-example>.py
```

## Using the repository from source

If you'd like to use the repository from source, you can either install from git or link to a cloned repository:

To install via git:

```sh
pip install git+ssh://git@github.com/checkrd/checkrd.git#subdirectory=wrappers/api-clients/python
```

Alternatively, you can build from source and install the wheel via `pip install path/to/wheel`. Building this package will create two files in the `dist/` folder, a `.tar.gz` and a `.whl`. You can install either one.

```sh
pip install build
python -m build wrappers/api-clients/python
pip install ./wrappers/api-clients/python/dist/checkrd_api-0.0.1.whl
```

If you'd like to install the package from a local checkout, you can install in editable mode:

```sh
pip install -e ./wrappers/api-clients/python
```

## Running tests

Most tests require you to [set up a mock server](https://github.com/stoplightio/prism) against the OpenAPI spec to run the tests.

```sh
# you will need npm installed
npx prism mock schemas/api/openapi.json
```

```sh
pytest wrappers/api-clients/python/tests
```

## Linting and formatting

This repository uses [ruff](https://github.com/astral-sh/ruff) and
[mypy](https://github.com/python/mypy) for formatting, linting, and type-checking. To lint:

```sh
ruff check wrappers/api-clients/python
mypy wrappers/api-clients/python
```

## Publishing and releases

This package is generated from the OpenAPI spec and currently has no automated
publish pipeline. The `CHANGELOG.md` is maintained by hand if releases resume.
