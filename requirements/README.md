# Lockfiles

Exact, hash-verified pins for this repository's **own** CI and release pipeline, required by
Packaging and Release Standards §4 and Security Standards §11.

| File | Contents | Used by |
|---|---|---|
| `release.in` / `release.lock` | The build and publish chain (`build`, `hatchling`, `twine`) | `release.yml`, both jobs, and CI's `build` job |
| `ci.lock` | Runtime dependencies plus the `dev` extra: the whole test, lint, type and boundary toolchain | Every blocking CI job — **not yet committed; see below** |

## What these are not

They do **not** define what a consumer installs. `pip install freeweight` resolves the compatible
ranges in `pyproject.toml`; an application that shipped pinned runtime dependencies would be
un-coinstallable with the rest of the suite. These files exist so that a green build stays green:
without them every CI run re-resolves, and a new `ruff` or `mypy` release can change the result
with no commit to explain it — and `pip-audit` would be auditing today's resolution rather than
what the build actually used.

## `ci.lock` is owed, and is blocked on `setspec 0.3.0`

`pyproject.toml` pins `setspec>=0.3,<0.4` (the M3 contract freeze). That version is not on PyPI
yet, so `pip-compile` resolves it from the workspace checkout and writes **hashes for a locally
built artifact** — hashes that cannot match the wheel CI publishes from the tag, which would make
every `--require-hashes` install fail with a mismatch. A lock like that is worse than no lock: it
looks authoritative and is wrong.

So `ci.lock` lands in the commit *after* `setspec 0.3.0` is published, and until then CI's
non-build jobs install from the ranges (`pip install -e ".[dev]"`), exactly as they did before.
`release.lock` has no such dependency — `build`, `hatchling` and `twine` are all on PyPI — so it
is committed now and `release.yml` uses it.

## Regenerating

Run after any change to `pyproject.toml`'s dependencies or `dev` extra, and commit the result:

```bash
pip install pip-tools

# Once setspec 0.3.0 is on PyPI:
pip-compile --strip-extras --extra dev --generate-hashes \
    --unsafe-package freeweight \
    --output-file requirements/ci.lock pyproject.toml

pip-compile --strip-extras --generate-hashes \
    --output-file requirements/release.lock requirements/release.in
```

`uv pip compile` is the sanctioned alternative (Security Standards §11).

**`--unsafe-package freeweight` is required and is not boilerplate.** This project's `dev` extra
depends on `freeweight[postgresql]` — a self-reference, so that one `pip install -e ".[dev]"` also
brings the PostgreSQL driver a developer needs for the `db-matrix` job. `pip-compile` resolves that
self-reference to `freeweight @ file:///…`, a local path that cannot be hashed and that would name
one developer's checkout in a committed file. Excluding the project from its own lock is the fix;
`psycopg` still appears, because it is reached through the extra either way.

## Interpreter

Resolved on Python 3.13. Every pin's `requires-python` admits 3.12, and no pin is
CPython-ABI-specific, so the same lock installs on both supported versions; the 3.14 early-warning
job deliberately resolves from ranges instead, because pinning a version that has no 3.14 wheels
would defeat the purpose of an early warning.
