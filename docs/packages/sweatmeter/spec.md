# SweatMeter — Specification

**Type:** Python package · **Import/distribution name:** `sweatmeter` · **Layer:** 3 (capability package)
**Status:** Specified, not implemented. **Decision record:** [ADR-0021](../../adr/0021-telemetry-collection-strategy.md).

---

## 1. Purpose

Report what the machine is doing and what the machine *is* — CPU, RAM, GPU, VRAM, temperatures,
power, clocks, disks — accurately, cheaply, and without ever inventing a number. One implementation
for the whole suite, so a benchmark's VRAM reading and a router's admission check mean the same thing.

## 2. Scope

* Live telemetry snapshots (`TelemetrySnapshot`) and a background sampler.
* Static machine profiling, producing `baseaicore.MachineProfile` with its fingerprint.
* Linux host readers (`/proc`, `/sys`) and NVIDIA GPU readers (`nvidia-smi`).
* Graceful per-field degradation and platform isolation.
* Derived helpers used by consumers: peak/mean over a window, energy integration from power samples,
  throttle detection.

## 3. Explicit non-goals

* No persistence. Consumers decide what to store and when
  ([FreeWeight](../../apps/freeweight/spec.md) persists during runs only).
* No HTTP, no UI, no charting.
* No alerting, thresholds or policy ("is 90 °C too hot?" is a consumer's judgement).
* No process-level accounting for other processes beyond the current process's own RSS.
* No control: no fan curves, no clock setting, no power limits.
* No benchmark or routing logic.
* No third-party runtime dependency.

## 4. Responsibilities

| Responsibility | Detail |
|---|---|
| Snapshot | One timestamped reading of every available live metric |
| Sampler | Background thread at a configurable interval, callback or iterator, clean shutdown |
| Machine profile | Static facts + fingerprint, separate from live utilization |
| Degradation | One failing sensor degrades one field to `UNSUPPORTED`; `snapshot()` never raises |
| Platform isolation | `HostReader` / `GpuReader` protocols with a factory; stubs for tier-3 platforms |
| Derived metrics | Window aggregates, energy integration, throttle heuristics — clearly labelled as estimates |
| Testability | Every reader injectable; all parsing testable from fixture text |

## 5. Dependencies

`baseaicore`. No third-party runtime dependency. Optional extra: `pynvml` (the NVML GPU reader,
selected automatically when installed — see §7), `psutil` (future non-Linux host readers).

## 6. Consumers

FreeWeight (run telemetry, machine provenance, energy and peak-VRAM metrics), LoadCoach (admission
control, dashboards, residency reasoning), IdeaPress (optional status display), external tools.

## 7. Public API

```python
# Live
@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    timestamp: datetime                       # timezone-aware UTC
    cpu_percent: Measurement
    load_average_1m: Measurement
    ram_used_bytes: Measurement
    ram_available_bytes: Measurement
    ram_total_bytes: Measurement
    cpu_temperature_c: Measurement
    disk_read_bytes_per_sec: Measurement
    disk_write_bytes_per_sec: Measurement
    process_rss_bytes: Measurement
    gpus: tuple[GpuSample, ...]

@dataclass(frozen=True, slots=True)
class GpuSample:
    index: int
    uuid: str | None
    utilization_percent: Measurement
    memory_utilization_percent: Measurement
    vram_used_bytes: Measurement
    vram_total_bytes: Measurement
    temperature_c: Measurement
    memory_temperature_c: Measurement
    power_watts: Measurement
    power_limit_watts: Measurement
    fan_percent: Measurement
    core_clock_mhz: Measurement
    memory_clock_mhz: Measurement
    throttle_reasons: tuple[str, ...]         # empty when unavailable *and* when none apply —
                                              # `throttle_reasons_available` distinguishes them
    throttle_reasons_available: bool

# Collection
class TelemetryCollector:
    def __init__(self, *, host: HostReader | None = None, gpu: GpuReader | None = None,
                 clock: Clock = utc_now) -> None: ...
    def snapshot(self) -> TelemetrySnapshot: ...      # never raises
    def machine_profile(self) -> MachineProfile: ...  # never raises

class TelemetrySampler:
    def __init__(self, collector: TelemetryCollector, *, interval_seconds: float = 1.0,
                 on_sample: Callable[[TelemetrySnapshot], None] | None = None) -> None: ...
    def start(self) -> None: ...
    def stop(self, *, timeout: float = 5.0) -> None: ...
    def latest(self) -> TelemetrySnapshot | None: ...
    def __iter__(self) -> Iterator[TelemetrySnapshot]: ...
    def __enter__/__exit__                            # context manager, always stops

# Platform interfaces
class HostReader(Protocol):
    def cpu_percent(self) -> Measurement: ...
    def memory(self) -> MemoryReading: ...
    def cpu_temperature(self) -> Measurement: ...
    def disk_throughput(self) -> DiskThroughput: ...
    def static_facts(self) -> HostFacts: ...

class GpuReader(Protocol):
    def available(self) -> bool: ...
    def sample(self) -> Sequence[GpuSample]: ...
    def static_info(self) -> Sequence[GpuProfile]: ...

create_host_reader(*, platform_name: str | None = None) -> HostReader
create_gpu_reader(*, prefer: GpuBackend | None = None) -> GpuReader
# GpuBackend.NVIDIA_SMI  — the bounded command; always available wherever a driver is.
# GpuBackend.PYNVML      — in-process NVML; needs the optional extra, no subprocess per sample.
# prefer=None selects PYNVML when the extra is importable and NVIDIA_SMI otherwise. Both produce
# identical GpuSample and GpuProfile values for the same device, proven by one conformance suite
# run against both (ADR-0021 §7); an explicit PYNVML request without the extra raises
# DependencyUnavailableError rather than silently downgrading.
NullHostReader()     # every field UNSUPPORTED with reason "platform_unsupported"; never zeros
NullGpuReader()      # available() is False; sample() is empty
# Both are public, because §13's degradation path tells consumers to construct one when
# create_host_reader raises. A degradation path that depends on a private symbol is not a path.

# Derived (explicitly labelled estimates)
class TelemetryWindow:
    def __init__(self, samples: Sequence[TelemetrySnapshot]) -> None: ...
    def peak_vram_bytes(self, gpu_index: int = 0) -> Measurement: ...
    def mean_power_watts(self, gpu_index: int = 0) -> Measurement: ...
    def energy_joules(self, gpu_index: int = 0) -> Measurement: ...   # Σ(power × dt), estimate
    def max_temperature_c(self, gpu_index: int = 0) -> Measurement: ...
    def suspected_throttling(self, gpu_index: int = 0) -> ThrottleVerdict: ...
    def sample_count(self) -> int

# Test doubles, shipped as supported API (Testing Standards §7)
sweatmeter.testing.ScriptedHostReader(samples: Sequence[HostReading])
sweatmeter.testing.ScriptedGpuReader(samples: Sequence[Sequence[GpuSample]])
sweatmeter.testing.FaultInjectingReader(wrapped, *, fail: str)
# LoadCoach's admission control and FreeWeight's energy integration both need a deterministic
# telemetry series; without these each application would monkeypatch the readers, which the
# testing standards call a smell.
```

**No method aggregates across GPUs.** Every derived figure takes a `gpu_index` and describes one
device; there is no machine-wide VRAM, power or energy total, because weights live on one device and
a sum describes no hardware that exists
([ADR-0027](../../adr/0027-multi-gpu-semantics.md)).

## 8. Inputs

Filesystem paths under `/proc` and `/sys` (injectable), the `nvidia-smi` executable (injectable
runner), a clock (injectable). No configuration file, no environment variable.

## 9. Outputs

`TelemetrySnapshot`, `GpuSample`, `MachineProfile`, `GpuProfile`, derived window aggregates.

## 10. Data ownership

None. The sampler holds only the most recent snapshot (and an optional bounded ring buffer the
consumer requests explicitly).

## 11. Public contracts

1. `snapshot()` and `machine_profile()` **never raise**. Any failure degrades exactly one field.
2. Unavailable is `UNSUPPORTED`, never zero.
3. Static identity and live utilization never appear in the same object.
4. The machine fingerprint excludes driver/toolkit versions and storage
   ([Machine Identity](../../architecture/machine-identity-and-reproducibility.md)).
5. Units are normalized and named: bytes, percent (0–100), °C, watts, MHz, bytes/second.
6. `energy_joules` is documented as a telemetry-derived **estimate**, never as instrumentation.
7. The sampler's overhead stays within budget and it always stops cleanly.
8. `throttle_reasons_available` distinguishes "no throttling" from "cannot tell".
9. Per-device figures are never summed or averaged into a machine-wide number by this package, and
   consumers are documented not to do it either.

## 12. Configuration

Constructor arguments only: sampling interval, `nvidia-smi` path and timeout, `/proc` and `/sys`
roots, preferred GPU backend, optional ring-buffer size.

## 13. Error behaviour

| Condition | Behaviour |
|---|---|
| `/proc` file unreadable | That field `UNSUPPORTED`; DEBUG log; snapshot continues |
| `nvidia-smi` absent | `GpuReader.available() == False`; `gpus == ()`; recorded reason |
| `nvidia-smi` non-zero exit or timeout | `gpus == ()` for that sample, reason recorded; the next sample retries |
| `nvidia-smi` malformed CSV | Parseable rows kept; unparseable fields `UNSUPPORTED` |
| Sensor missing (power/fan/clock/temp) | That field `UNSUPPORTED` |
| NVML sensor unsupported on the device | That field `UNSUPPORTED` with its reason; every other field on the same device is unaffected, because each is its own call |
| `nvmlInit` fails (no driver, no permission) | `available() == False`; `gpus == ()`; reason recorded; the next call retries initialization |
| `GpuBackend.PYNVML` requested without the extra | `create_gpu_reader` raises `DependencyUnavailableError` naming the extra; never a silent downgrade |
| Permission denied | Field `UNSUPPORTED`, reason `permission_denied` |
| Unsupported platform | `create_host_reader` raises `UnsupportedPlatformError`; consumers construct a `NullHostReader` and degrade |
| Sampler thread dies | Logged at ERROR by the consumer's handler; `latest()` keeps returning the last snapshot with its (now stale) timestamp so staleness is visible |

## 14. Security considerations

* Read-only. Nothing is written, nothing is controlled.
* `nvidia-smi` invoked with an explicit argument list, resolved via `shutil.which`, never through a
  shell, always with a timeout.
* Output size is capped before parsing.
* Hostname appears in the machine profile; consumers decide whether to export it. Documented.
* No network access of any kind.

## 15. Performance

| Measure | Target | Ceiling |
|---|---|---|
| Snapshot, no GPU | ≤ 3 ms | 10 ms |
| Snapshot with `nvidia-smi`, 1 GPU | ≤ 40 ms | 120 ms |
| Snapshot with `pynvml` (NVML), 1 GPU | ≤ 3 ms | 10 ms |
| Sampler CPU at 1 s interval | ≤ 0.5 % of one core | 1.5 % |
| Effect on measured benchmark throughput | ≤ 1 % | 2 % |
| Memory | ≤ 5 MB steady | — |

The last row is the one that matters most: telemetry must not distort the measurement it documents.
FreeWeight runs a calibration test (sampling on vs off) and records the delta on the run.

## 16. Cross-platform

Linux is tier 1. Windows and macOS are tier 3: `HostReader` stubs raise `UnsupportedPlatformError`
with a message naming what is missing; `GpuReader` still works wherever `nvidia-smi` is on PATH.
Every parser is tested from fixture text on every platform, so Linux parsing is fully covered in CI
regardless of the runner. See [Cross-Platform Standards](../../standards/cross-platform-standards.md).

## 17. Observability

* Library-level DEBUG logs only, under `sweatmeter.*`, for reader failures and their reasons.
* Every degraded field carries a reason retrievable via `snapshot.unavailable_reasons()` so consumers
  can display "power sensor unavailable" rather than a bare `—`.
* No INFO+ logging from the library.

## 18. Test strategy

| Area | Tests |
|---|---|
| `/proc` parsers | `stat` (delta computation, first-call behaviour), `meminfo`, `loadavg`, `diskstats`, `cpuinfo` (multi-socket, ARM), malformed and truncated inputs |
| `/sys` parsers | thermal zones, hwmon variants, absent trees, permission errors, block devices |
| `nvidia-smi` | Single GPU, multi-GPU, missing columns, `[N/A]` values, non-numeric values, empty output, non-zero exit, timeout, malformed CSV, driver-version variants |
| Degradation | Fault-injecting reader fails each field in turn; `snapshot()` never raises and only that field degrades |
| Fingerprint | Delegated to BaseAiCore; here: correct assembly from reader output, GPU sorting, `UNSUPPORTED` handling |
| Sampler | Start/stop, interval accuracy, clean shutdown, exception in callback isolated, context manager, `latest()` staleness, no thread leak |
| Derived | Energy integration against a known series, peak/mean, throttle heuristic, empty window, all-`UNSUPPORTED` window |
| GPU backend conformance | One suite of protocol invariants — value types, index ordering, unit ranges, honest throttle reporting — run against every `GpuReader`: recorded `nvidia-smi` output, the NVML backend over a fake binding, `NullGpuReader` and `ScriptedGpuReader`; plus an equivalence test pinning both NVIDIA backends to identical output for one device ([ADR-0021](../../adr/0021-telemetry-collection-strategy.md) §7) |
| Platform factory | Every branch including the unsupported-platform error; stubs raise, never return zeros; `NullHostReader` degrades every field to `UNSUPPORTED` with a reason and never to `0` |
| Test doubles | `ScriptedHostReader`/`ScriptedGpuReader` replay a fixed series deterministically; `FaultInjectingReader` fails the named field and only that field |
| Performance | Snapshot latency, sampler overhead, memory |
| Live (marked) | Real machine: plausible values, GPU present, ≥ 2 GPUs where available |

Coverage floor: **95 %**. The default suite must pass on a machine with no GPU.

## 19. Compatibility and versioning

* Semantic versioning; pre-1.0 `0.x`.
* Adding a metric field is a minor bump (dataclasses are frozen; consumers use attribute access).
* Changing a unit or a field's meaning is major — units are part of the contract.
* `nvidia-smi` fixtures are annotated with the driver version they came from; a new driver adds a
  fixture rather than replacing one.

## 20. Acceptance criteria

1. A five-line script prints CPU, RAM, GPU and VRAM using only `sweatmeter` and `baseaicore`.
2. The full test suite passes on a machine with **no** GPU and no `nvidia-smi`.
3. A fault-injection test proves no single sensor failure can raise out of `snapshot()`.
4. Multi-GPU output parses correctly from fixtures.
5. Missing power/temperature yields `UNSUPPORTED` with a reason, never `0`.
6. Sampler overhead within budget; effect on throughput ≤ 1 % measured.
7. `mypy --strict`, `ruff`, `lint-imports` clean; coverage ≥ 95 %.

## 21. Future extensions

* AMD via `rocm-smi`; Intel GPUs; Apple Silicon via `powermetrics`.
* Windows and macOS `HostReader`s (likely on `psutil` as an extra).
* Per-process GPU memory attribution where the driver exposes it.
* Network throughput, if a consumer needs it.
* RAPL-based CPU energy on Linux, as an additional estimate with its own availability flag.
