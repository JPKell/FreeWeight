# FreeWeight configuration reference

**Generated from `freeweight.config.Settings` by
`scripts/generate_config_reference.py`.**
Do not edit by hand: CI fails when this file differs from what the model generates.

Precedence, lowest to highest: built-in defaults → `config.toml` → `FREEWEIGHT_*`
environment variables → CLI flags (configuration standards §1). Overriding is per leaf
field, never per section. Database-backed runtime settings sit between the file and the
environment (§7). The file lives at `$XDG_CONFIG_HOME/freeweight/config.toml`;
`freeweight config path` prints the resolved location and `freeweight config init` writes
a commented example.

Environment variables spell the key path as `FREEWEIGHT_<SECTION>__<FIELD>`; a list is
comma-separated. Two conveniences exist outside that scheme: `FREEWEIGHT_CONFIG` names
the file and `FREEWEIGHT_DATA_DIR` moves the data directory.

**Runtime-changeable** means the settings page and `PUT /api/v1/settings` may change it
and the change applies to work started from then on. Everything else is read once at
startup. **Config only** keys decide who can reach this machine, what leaves it, or where
its data lives; the settings API refuses them with `FORBIDDEN` (configuration standards
§7).

## `[server]`

Bind address and HTTP-level limits.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `server.host` | `FREEWEIGHT_SERVER__HOST` | string | `"127.0.0.1"` | — | no — file or environment, then restart | Config only. A non-loopback bind exposes the service beyond this machine (ADR-0026). | `"127.0.0.1"` | Interface to bind. Loopback by default; anything else requires allowed_hosts and auth.tokens (ADR-0026). |
| `server.port` | `FREEWEIGHT_SERVER__PORT` | integer | `8765` | ≥ 1, ≤ 65535 | no — file or environment, then restart | Config only. Part of the bind. | `8765` | TCP port for the web UI and the API. |
| `server.allow_lan_exposure` | `FREEWEIGHT_SERVER__ALLOW_LAN_EXPOSURE` | boolean | `false` | — | no — file or environment, then restart | Config only. The acknowledgement that makes a `0.0.0.0` bind deliberate. | `false` | Acknowledges a deliberate bind to every interface (0.0.0.0). Without it such a bind refuses to start. |
| `server.allowed_hosts` | `FREEWEIGHT_SERVER__ALLOWED_HOSTS` | list of string | `[]` | — | no — file or environment, then restart | Config only. Defends a non-loopback bind against DNS rebinding. | `["bench.local"]` | Host header values accepted on a non-loopback bind, against DNS rebinding. Comma-separated in the environment. |
| `server.request_timeout_seconds` | `FREEWEIGHT_SERVER__REQUEST_TIMEOUT_SECONDS` | number | `120.0` | > 0 | no — file or environment, then restart | — | `120.0` | Server-side request timeout. |

## `[storage]`

Database and artifact locations.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `storage.database_url` | `FREEWEIGHT_STORAGE__DATABASE_URL` | string, optional | unset | — | no — file or environment, then restart | Config only. May carry a PostgreSQL password; redacted by `config show`. | `"sqlite:////var/lib/freeweight/freeweight.sqlite3"` | SQLAlchemy URL. Unset resolves to a SQLite file under the XDG data directory; PostgreSQL is the other supported dialect (ADR-0006). |
| `storage.auto_migrate` | `FREEWEIGHT_STORAGE__AUTO_MIGRATE` | boolean | `true` | — | no — file or environment, then restart | — | `true` | Migrate on startup. Unset means true on SQLite and false on PostgreSQL, where a failed migration cannot be rolled back automatically (database standards §5.1). |
| `storage.artifact_dir` | `FREEWEIGHT_STORAGE__ARTIFACT_DIR` | string, optional | unset | — | no — file or environment, then restart | Config only. Where raw responses and generated code are written. | `"/var/lib/freeweight/artifacts"` | Where run artifacts (raw responses, generated code, exports) are written. Unset resolves under the XDG data directory. |
| `storage.backup_retention` | `FREEWEIGHT_STORAGE__BACKUP_RETENTION` | integer | `5` | ≥ 0 | yes — applies to work started from now on | — | `5` | Automatic pre-migration backups kept before the oldest is rotated away. |
| `storage.statement_timeout_ms` | `FREEWEIGHT_STORAGE__STATEMENT_TIMEOUT_MS` | integer, optional | unset | > 0 | no — file or environment, then restart | — | `30000` | PostgreSQL statement (and lock) timeout. Unset leaves the server default; SQLite uses its own busy timeout, which the engine always sets. |

## `[provider]`

The default model provider FreeWeight talks to.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `provider.kind` | `FREEWEIGHT_PROVIDER__KIND` | string | `"ollama"` | — | no — file or environment, then restart | — | `"ollama"` | Which provider serves the models: ollama, or fake for tests. |
| `provider.base_url` | `FREEWEIGHT_PROVIDER__BASE_URL` | string | `"http://127.0.0.1:11434"` | — | no — file or environment, then restart | Config only. Where prompts are sent. | `"http://127.0.0.1:11434"` | The provider's API endpoint. |
| `provider.timeout_seconds` | `FREEWEIGHT_PROVIDER__TIMEOUT_SECONDS` | number | `300.0` | > 0 | no — file or environment, then restart | — | `300.0` | Per-call provider timeout. |

## `[providers]`

Cross-provider policy, distinct from the single default provider's own settings.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `providers.allow_remote` | `FREEWEIGHT_PROVIDERS__ALLOW_REMOTE` | boolean | `false` | — | no — file or environment, then restart | Config only. Lets content leave this machine; one half of the remote opt-in. | `false` | Permit a remote provider at all. One half of the remote-judging opt-in; judge.allow_remote is the other (ADR-0031 §4). |

## `[telemetry]`

Sampling behaviour for the (Phase 4) telemetry bar and (Phase 6) run recording.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `telemetry.interval_ms` | `FREEWEIGHT_TELEMETRY__INTERVAL_MS` | integer | `1000` | > 0 | yes — applies to work started from now on | — | `1000` | How often the sampler reads the host and the GPUs. Recorded on every run as a measurement condition. |
| `telemetry.persist_during_runs` | `FREEWEIGHT_TELEMETRY__PERSIST_DURING_RUNS` | boolean | `true` | — | yes — applies to work started from now on | — | `true` | Store the telemetry series beside a run so its charts survive a restart. |
| `telemetry.calibrate_overhead` | `FREEWEIGHT_TELEMETRY__CALIBRATE_OVERHEAD` | boolean | `true` | — | yes — applies to work started from now on | — | `true` | Measure what sampling itself costs before a run, and record it on the run. |

## `[execution]`

Default benchmark execution parameters (spec §12, ``[execution]``).

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `execution.warmup_repetitions` | `FREEWEIGHT_EXECUTION__WARMUP_REPETITIONS` | integer | `1` | ≥ 0 | yes — applies to work started from now on | — | `1` | Unmeasured generations before the measured ones. |
| `execution.measured_repetitions` | `FREEWEIGHT_EXECUTION__MEASURED_REPETITIONS` | integer | `3` | ≥ 1 | yes — applies to work started from now on | — | `3` | How many times each case runs. |
| `execution.cooldown_seconds` | `FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS` | number | `5.0` | ≥ 0 | yes — applies to work started from now on | — | `5.0` | Idle gap between tests. |
| `execution.test_timeout_seconds` | `FREEWEIGHT_EXECUTION__TEST_TIMEOUT_SECONDS` | number | `600.0` | > 0 | no — file or environment, then restart | — | `600.0` | Per-provider-call timeout. |
| `execution.run_timeout_seconds` | `FREEWEIGHT_EXECUTION__RUN_TIMEOUT_SECONDS` | number | `86400.0` | > 0 | no — file or environment, then restart | — | `86400.0` | Total budget for one run. |
| `execution.randomize_case_order` | `FREEWEIGHT_EXECUTION__RANDOMIZE_CASE_ORDER` | boolean | `true` | — | yes — applies to work started from now on | — | `true` | Shuffle case order within a test. |
| `execution.seed` | `FREEWEIGHT_EXECUTION__SEED` | integer | `0` | — | yes — applies to work started from now on | — | `0` | The seed every randomized decision derives from; recorded on every run. |
| `execution.gpu_index` | `FREEWEIGHT_EXECUTION__GPU_INDEX` | integer | `0` | ≥ 0 | no — file or environment, then restart | — | `0` | The device a run's metrics are attributed to (ADR-0027). |
| `execution.idle_gpu_threshold_percent` | `FREEWEIGHT_EXECUTION__IDLE_GPU_THRESHOLD_PERCENT` | number | `10.0` | ≥ 0, ≤ 100 | yes — applies to work started from now on | — | `10.0` | GPU utilization the machine must be below before measuring; 0 disables the check and records that it was disabled. |
| `execution.idle_required_samples` | `FREEWEIGHT_EXECUTION__IDLE_REQUIRED_SAMPLES` | integer | `3` | ≥ 1 | no — file or environment, then restart | — | `3` | Consecutive quiet observations required before measuring. |
| `execution.idle_wait_timeout_seconds` | `FREEWEIGHT_EXECUTION__IDLE_WAIT_TIMEOUT_SECONDS` | number | `120.0` | ≥ 0 | no — file or environment, then restart | — | `120.0` | How long to wait for the machine to go quiet. |
| `execution.on_idle_timeout` | `FREEWEIGHT_EXECUTION__ON_IDLE_TIMEOUT` | one of `"warn"`, `"refuse"` | `"warn"` | listed values | yes — applies to work started from now on | — | `"warn"` | What to do when it never does: warn (measure anyway and record measured_while_busy) or refuse. |
| `execution.store_responses` | `FREEWEIGHT_EXECUTION__STORE_RESPONSES` | boolean | `false` | — | no — file or environment, then restart | — | `false` | Store full response text beside its hash. Off by default (spec §14); goal runs force it on. |

## `[runtime]`

``[runtime]`` — how a model is loaded and served, as opposed to how a run is executed.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `runtime.context_size` | `FREEWEIGHT_RUNTIME__CONTEXT_SIZE` | integer, optional | unset | > 0 | no — file or environment, then restart | — | `8192` | Context window to serve, in tokens (Ollama's num_ctx). Unset lets the provider decide and records the served context as assumed. A different value is a different measurement subject (ADR-0023). |
| `runtime.gpu_layers` | `FREEWEIGHT_RUNTIME__GPU_LAYERS` | integer, optional | unset | ≥ 0 | no — file or environment, then restart | — | `32` | Layers offloaded to the GPU. Unset lets the provider fit them. |
| `runtime.threads` | `FREEWEIGHT_RUNTIME__THREADS` | integer, optional | unset | > 0 | no — file or environment, then restart | — | `8` | CPU threads for the parts that stay on the host. |
| `runtime.batch_size` | `FREEWEIGHT_RUNTIME__BATCH_SIZE` | integer, optional | unset | > 0 | no — file or environment, then restart | — | `512` | Prompt-evaluation batch size. |
| `runtime.keep_alive` | `FREEWEIGHT_RUNTIME__KEEP_ALIVE` | string, optional | unset | — | no — file or environment, then restart | — | `"5m"` | How long the provider holds the model resident after a call, in the provider's own duration syntax. |

## `[benchmarks]`

The ``[benchmarks]`` section: limits a machine, not a suite author, decides.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `benchmarks.long_context_max_tokens` | `FREEWEIGHT_BENCHMARKS__LONG_CONTEXT_MAX_TOKENS` | integer | `32000` | ≥ 1000, ≤ 2000000 | no — file or environment, then restart | — | `32000` | Ceiling of native.long_context's depth sweep. Hashed into that suite's dataset_hashes, so two ceilings are two measurements. |

## `[goals]`

Where user-authored goal packs live, and the bounds on what one may contain (spec §12).

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `goals.root` | `FREEWEIGHT_GOALS__ROOT` | string, optional | unset | — | no — file or environment, then restart | Config only. Where hand-editable goal packs are read from. | `"/home/me/.config/freeweight/goals"` | Where goal packs live. Unset resolves to <config>/goals. |
| `goals.max_pack_bytes` | `FREEWEIGHT_GOALS__MAX_PACK_BYTES` | integer | `5242880` | > 0 | no — file or environment, then restart | — | `5242880` | Import size cap, enforced before a byte is written. |
| `goals.rule_timeout_ms` | `FREEWEIGHT_GOALS__RULE_TIMEOUT_MS` | integer | `250` | > 0 | no — file or environment, then restart | — | `250` | Per-criterion, per-sample budget for a rule. |

## `[sandbox]`

The ``[sandbox]`` section: how model-generated code is contained (spec §12, ADR-0018).

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `sandbox.tier` | `FREEWEIGHT_SANDBOX__TIER` | one of `"auto"`, `"container"`, `"bwrap"`, `"none"` | `"auto"` | listed values | no — file or environment, then restart | Config only: refused by the settings API and the UI. | `"auto"` | Sandbox tier for code-execution benchmarks: auto = highest available; container and bwrap select exactly that tier or refuse; none refuses all code execution. |
| `sandbox.cpu_limit` | `FREEWEIGHT_SANDBOX__CPU_LIMIT` | integer | `2` | ≥ 1, ≤ 256 | no — file or environment, then restart | — | `2` | CPU cores a sandboxed process may use. |
| `sandbox.memory_limit_mb` | `FREEWEIGHT_SANDBOX__MEMORY_LIMIT_MB` | integer | `2048` | ≥ 64 | no — file or environment, then restart | — | `2048` | Memory cap for sandboxed execution, in MiB. |
| `sandbox.timeout_seconds` | `FREEWEIGHT_SANDBOX__TIMEOUT_SECONDS` | integer | `30` | ≥ 1 | no — file or environment, then restart | — | `30` | Wall-clock budget per sandboxed invocation, in seconds. |

## `[external]`

The ``[external]`` section: where external benchmark environments live (spec §12, ADR-0018).

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `external.root` | `FREEWEIGHT_EXTERNAL__ROOT` | string, optional | unset | — | no — file or environment, then restart | Config only: refused by the settings API and the UI. | `"/home/me/.local/share/freeweight/external"` | Where external benchmark environments live. Unset resolves to <data>/external. |
| `external.install_timeout_seconds` | `FREEWEIGHT_EXTERNAL__INSTALL_TIMEOUT_SECONDS` | integer | `1800` | ≥ 1 | no — file or environment, then restart | — | `1800` | Budget for one install or download step, in seconds. |
| `external.download_cap_bytes` | `FREEWEIGHT_EXTERNAL__DOWNLOAD_CAP_BYTES` | integer | `2147483648` | > 0 | no — file or environment, then restart | — | `2147483648` | Streaming size cap for a single dataset download, in bytes. |

## `[judge]`

The default jury a goal's judged criteria are scored by (spec §12).

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `judge.jury_size` | `FREEWEIGHT_JUDGE__JURY_SIZE` | integer | `3` | ≥ 1 | no — file or environment, then restart | — | `3` | Distinct local models in a jury; 1 disables the jury and says so in the result. |
| `judge.models` | `FREEWEIGHT_JUDGE__MODELS` | list of string | `[]` | — | no — file or environment, then restart | — | `["ollama/qwen3.5:14b"]` | Juror canonical IDs. Empty selects from the installed models. Comma-separated in the environment. |
| `judge.repetitions` | `FREEWEIGHT_JUDGE__REPETITIONS` | integer | `3` | ≥ 1 | no — file or environment, then restart | — | `3` | How many times each juror grades each criterion. |
| `judge.randomize_order` | `FREEWEIGHT_JUDGE__RANDOMIZE_ORDER` | boolean | `true` | — | no — file or environment, then restart | — | `true` | Randomize the order criteria are presented to a juror in. |
| `judge.blind_candidate_identity` | `FREEWEIGHT_JUDGE__BLIND_CANDIDATE_IDENTITY` | boolean | `true` | — | no — file or environment, then restart | — | `true` | Hide the candidate's identity from the jury. |
| `judge.refuse_self_judging` | `FREEWEIGHT_JUDGE__REFUSE_SELF_JUDGING` | boolean | `true` | — | no — file or environment, then restart | — | `true` | A juror never judges its own output. |
| `judge.allow_remote` | `FREEWEIGHT_JUDGE__ALLOW_REMOTE` | boolean | `false` | — | no — file or environment, then restart | Config only. Lets a candidate's output leave this machine to be judged. | `false` | Permit a remote juror. Requires providers.allow_remote as well. |
| `judge.temperature` | `FREEWEIGHT_JUDGE__TEMPERATURE` | number | `0.0` | ≥ 0.0, ≤ 2.0 | no — file or environment, then restart | — | `0.0` | Sampling temperature every juror is polled at. |

## `[calibration]`

How judged criteria are calibrated against the author's grades (spec §12).

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `calibration.target_samples` | `FREEWEIGHT_CALIBRATION__TARGET_SAMPLES` | integer | `12` | ≥ 1 | no — file or environment, then restart | — | `12` | How many samples the wizard asks the author to grade. |
| `calibration.min_samples` | `FREEWEIGHT_CALIBRATION__MIN_SAMPLES` | integer | `8` | ≥ 1 | no — file or environment, then restart | — | `8` | Below this many graded samples: CALIBRATION_INSUFFICIENT, not a failed gate. |
| `calibration.holdout_fraction` | `FREEWEIGHT_CALIBRATION__HOLDOUT_FRACTION` | number | `0.4` | > 0.0, < 1.0 | no — file or environment, then restart | — | `0.4` | Share of graded samples withheld from the jury — the only honest estimate of agreement. |
| `calibration.partition_seed` | `FREEWEIGHT_CALIBRATION__PARTITION_SEED` | integer | `0` | — | no — file or environment, then restart | — | `0` | Seed of the anchor/holdout split, recorded so the split is reproducible. |
| `calibration.min_agreement` | `FREEWEIGHT_CALIBRATION__MIN_AGREEMENT` | number | `0.4` | ≥ -1.0, ≤ 1.0 | no — file or environment, then restart | — | `0.4` | Weighted kappa_w below which a goal emits no evidence at all (ADR-0032 §3). |
| `calibration.n_holdout_target` | `FREEWEIGHT_CALIBRATION__N_HOLDOUT_TARGET` | integer | `10` | ≥ 1 | no — file or environment, then restart | — | `10` | Shrinkage denominator for judge_validity_factor (ADR-0032 §2). |

## `[evidence]`

``[evidence]`` — the confidence policy and where the capability weights come from.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `evidence.n_target` | `FREEWEIGHT_EVIDENCE__N_TARGET` | integer | `30` | ≥ 1 | no — file or environment, then restart | — | `30` | Sample count at which the sample factor reaches 1.0 (ADR-0017): three samples are worth about 0.32, thirty about 1.0. |
| `evidence.quality_half_life_days` | `FREEWEIGHT_EVIDENCE__QUALITY_HALF_LIFE_DAYS` | number | `90.0` | > 0 | no — file or environment, then restart | — | `90.0` | Freshness half-life for quality evidence, in days. Quality is stable while the weights are. |
| `evidence.performance_half_life_days` | `FREEWEIGHT_EVIDENCE__PERFORMANCE_HALF_LIFE_DAYS` | number | `30.0` | > 0 | no — file or environment, then restart | — | `30.0` | Freshness half-life for performance, memory and energy evidence, in days. Speed follows the environment. |
| `evidence.freshness_floor` | `FREEWEIGHT_EVIDENCE__FRESHNESS_FLOOR` | number | `0.3` | ≥ 0.0, ≤ 1.0 | no — file or environment, then restart | — | `0.3` | The lowest the freshness factor can fall. Old evidence degrades rather than vanishes. |
| `evidence.stale_below` | `FREEWEIGHT_EVIDENCE__STALE_BELOW` | number | `0.5` | ≥ 0.0, ≤ 1.0 | no — file or environment, then restart | — | `0.5` | Freshness below which evidence is badged stale and offered a re-run — about one half-life. |
| `evidence.name_only_identity_factor` | `FREEWEIGHT_EVIDENCE__NAME_ONLY_IDENTITY_FACTOR` | number | `0.6` | ≥ 0.0, ≤ 1.0 | no — file or environment, then restart | — | `0.6` | Identity factor for a name-only model identity, whose weights may have changed under the name. |
| `evidence.performance_drift_factor` | `FREEWEIGHT_EVIDENCE__PERFORMANCE_DRIFT_FACTOR` | number | `0.7` | ≥ 0.0, ≤ 1.0 | no — file or environment, then restart | — | `0.7` | Environment factor for performance-class evidence after a provider minor change or a driver/CUDA change. |
| `evidence.quality_drift_factor` | `FREEWEIGHT_EVIDENCE__QUALITY_DRIFT_FACTOR` | number | `0.5` | ≥ 0.0, ≤ 1.0 | no — file or environment, then restart | — | `0.5` | Environment factor for quality evidence after a provider change with template or sampling implications. |
| `evidence.goal_contribution_weight` | `FREEWEIGHT_EVIDENCE__GOAL_CONTRIBUTION_WEIGHT` | number | `1.0` | > 0 | no — file or environment, then restart | — | `1.0` | Weight a goal's composite carries as one source among the shipped ones inside the capability it declares contributes_to (ADR-0032 §1). |
| `evidence.capability_weights_path` | `FREEWEIGHT_EVIDENCE__CAPABILITY_WEIGHTS_PATH` | string, optional | unset | — | no — file or environment, then restart | — | `"/home/me/.config/freeweight/capability_weights.toml"` | A custom capability_weights.toml. Unset uses the shipped mapping; a custom file derives its own policy version from its content. |

## `[auth]`

Bearer tokens. Empty is the loopback-default, unauthenticated posture (ADR-0014).

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `auth.tokens` | `FREEWEIGHT_AUTH__TOKENS` | list of string | `[]` | — | no — file or environment, then restart | Secret. Redacted by `config show`, never logged; required for a non-loopback bind. | `["********"]` | Bearer tokens accepted on a non-loopback bind. Secret: never logged, always redacted by config show. Comma-separated in the environment. |

## `[logging]`

Structured-logging behaviour.

| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | Security | Example | Meaning |
|---|---|---|---|---|---|---|---|---|
| `logging.level` | `FREEWEIGHT_LOGGING__LEVEL` | string | `"INFO"` | — | yes — applies to work started from now on | — | `"INFO"` | Log verbosity. |
| `logging.format` | `FREEWEIGHT_LOGGING__FORMAT` | one of `"text"`, `"json"`, `"auto"` | `"auto"` | listed values | no — file or environment, then restart | — | `"auto"` | text, json, or auto (text on a TTY, json otherwise). |
| `logging.include_content` | `FREEWEIGHT_LOGGING__INCLUDE_CONTENT` | boolean | `false` | — | no — file or environment, then restart | Config only. Logs full prompts and responses when on; hashes only when off. | `false` | Log full prompts and responses. Off by default: only hashes and lengths are logged (observability standards §3.2). |
