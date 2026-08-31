# Quickstart

FreeWeight measures local open-weight models — how well one performs on this machine, for a
capability, under a set of runtime settings — and preserves enough provenance to reproduce every
number. It runs with zero configuration.

## Install and start

```bash
pip install freeweight
freeweight serve
```

The web UI and the `/api/v1` API come up on `http://127.0.0.1:8765`. No configuration, no
credentials, nothing leaves the machine. Only a *run* needs a model provider — install
[Ollama](https://ollama.com) and pull a model if you have not:

```bash
ollama serve
ollama pull llama3.1:8b
```

## Your first benchmark

From the UI: open **Models**, click **Discover** (this asks Ollama what it has and records each
model's canonical identity), then **Runs → Start a run**, pick the model and a suite such as
`native.echo` or `native.performance`, and watch it stream. Every headline metric on the results
page drills to the raw sample that produced it in at most two clicks.

From the CLI:

```bash
freeweight models refresh                       # discover models through Ollama
freeweight run start --model <ref> --suite native.performance
freeweight run list
freeweight results show <run-id>
freeweight results export <run-id> --format json > result.json
```

## Health and diagnosis

```bash
freeweight health          # one line per component
freeweight doctor          # the same, with the cause and remedy of any problem
freeweight version
```

If anything is wrong, `freeweight doctor` names the component and points at
[troubleshooting](troubleshooting.md).

## Where things live

```text
~/.config/freeweight/config.toml   configuration (respects XDG_CONFIG_HOME)
~/.local/share/freeweight/         data root: the database, artifacts, exports, backups
~/.local/state/freeweight/logs/    logs
```

Every path is overridable. The full configuration surface — every key, its environment variable,
type, default and security note — is the generated [configuration reference](configuration.md).

## Next

* [Benchmarks](benchmarks.md) — the native suites, external adapters, and user-defined goals.
* [Backup and restore](backup-restore.md) — protecting your measurements.
* [Upgrading](upgrading.md) — moving to a new version with your data intact.
* [Security](security.md) — the local-first posture and what changes it.
