# Lockfiles

Exact, hash-verified pins for this repository's **own** CI and release pipeline, required by
Packaging and Release Standards §4 and Security Standards §11.

| File | Contents | Used by |
|---|---|---|
| `release.in` / `release.lock` | The build and publish chain (`build`, `hatchling`, `twine`) | `release.yml`, both jobs, and CI's `build` job |
| `ci.lock` | Runtime dependencies plus the `dev` and `postgresql` extras: the whole test, lint, type and boundary toolchain | Every blocking CI job (installed `--require-hashes`, then `pip install . --no-deps`) |

## What these are not

They do **not** define what a consumer installs. `pip install freeweight` resolves the compatible
ranges in `pyproject.toml`; an application that shipped pinned runtime dependencies would be
un-coinstallable with the rest of the suite. These files exist so that a green build stays green:
without them every CI run re-resolves, and a new `ruff` or `mypy` release can change the result
with no commit to explain it — and `pip-audit` would be auditing today's resolution rather than
what the build actually used.

## `ci.lock` is cut on Python 3.13

`ci.lock` is generated on `python3.13` (the middle of the 3.12/3.13 CI matrix) and resolves the
suite siblings from PyPI — `setspec 0.4.0`, `weightsdb 0.2.0`, `mirrorwall 0.2.0` — with their
published hashes. Every blocking CI job installs it with `--require-hashes` and then
`pip install . --no-deps`, so what CI tests is exactly what the lock resolved, measured against the
installed distribution rather than an editable tree. The **3.14 early-warning job is the one
deliberate exception**: pinning a version with no 3.14 wheels would defeat the early warning, so it
installs `-e ".[dev]"` from the ranges. `release.lock` covers the build/publish chain
(`build`, `hatchling`, `twine`); both locks are audited by `pip-audit --require-hashes` in the
`security` job.

## Regenerating

Run after any change to `pyproject.toml`'s dependencies or `dev` extra, and commit the result:

```bash
pip install pip-tools

# On python3.13 (the middle of the CI matrix):
pip-compile --strip-extras --extra dev --extra postgresql --generate-hashes \
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
