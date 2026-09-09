# FreeWeight

Measures local open-weight models across capability, efficiency, reliability and resource use, with reproducible provenance and exportable capability evidence.

**Status:** `1.2.0` in the repository (release prepared, untagged; `pip index versions freeweight` says what PyPI serves). Phases 1–15 built, through the adapter arc's LA3 checkpoint. Every
native suite runs and measures, subjective goals are authored, calibrated and scored from the UI,
and the application exports **capability evidence** — one `capability.evidence` record per model,
runtime profile, machine and capability, with ADR-0017's confidence beside the score — as a
`benchmark.evidence_bundle` a consumer reads with `setspec` alone. GGUF weights are served through
a llama.cpp server FreeWeight supervises, with base × adapter subjects measured and exported
individually; see the [development plan](docs/apps/freeweight/development-plan.md).

Reference documents generated from the code, checked in CI: the
[configuration reference](docs/configuration.md) and the [OpenAPI snapshot](docs/openapi.json).

Part of the **Local AI Suite**.

## Install

```bash
pip install freeweight
freeweight serve
```

Starts on `127.0.0.1:8765` with zero configuration. See [docs/apps/freeweight/spec.md](docs/apps/freeweight/spec.md) §12 for the full configuration surface and `FREEWEIGHT_*` environment variables.

## Quickstart

```bash
pip install freeweight
freeweight serve            # starts the web UI + API on 127.0.0.1:8765
freeweight health --json     # same health data the API reports, from the CLI
freeweight --help
```

## Compatibility

Declared version ranges from `pyproject.toml` — kept from drifting by
`tests/unit/test_readme_compatibility.py`, which parses the file and fails if this table disagrees:

| Package | Range |
|---|---|
| `baseaicore` | `>=0.4.2,<0.5` |
| `weightsdb` | `>=0.2,<0.3` |
| `mirrorwall` | `>=0.2.2,<0.3` |
| `setspec` | `>=0.6,<0.7` |
| `modelrack` | `>=0.8,<0.9` |
| `sweatmeter` | `>=0.4,<0.5` |

## Documentation

Project documentation lives under [`docs/`](docs/README.md). Start with [`docs/README.md`](docs/README.md).

| Read this | For |
|---|---|
| [docs/apps/freeweight/spec.md](docs/apps/freeweight/spec.md) | Purpose, scope, non-goals, public contracts, configuration, acceptance criteria |
| [docs/apps/freeweight/development-plan.md](docs/apps/freeweight/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -m "not live and not performance"
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and [`SECURITY.md`](SECURITY.md) for
how to report a vulnerability.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
