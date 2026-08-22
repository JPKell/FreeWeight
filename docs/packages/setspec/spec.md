# SetSpec — Specification

**Type:** Python package · **Import/distribution name:** `setspec` · **Layer:** 2 (contract package)
**Status:** Specified, not implemented. **Decision record:** [ADR-0009](../../adr/0009-setspec-schema-strategy.md).

---

## 1. Purpose

Own every data contract that crosses an application boundary. When FreeWeight exports benchmark
evidence and LoadCoach imports it, or when any application emits an event or an error envelope, the
shape, the version rules and the validation come from here — so that two independently released
applications can exchange data without sharing code or a database.

## 2. Scope

* Versioned payload models: benchmark results, run summaries, evidence bundles, capability evidence,
  machine profile exchange, model identity exchange, event envelopes, error envelopes.
* The schema envelope and version negotiation rules.
* The suite's capability vocabulary, versioned.
* JSON Schema generation and golden payload publication.
* Serialization and deserialization helpers, including `Unsupported` handling.

## 3. Explicit non-goals

* No scoring, aggregation or confidence computation (FreeWeight owns those; SetSpec only carries the
  results).
* No routing weights or task profiles (LoadCoach's, and application-internal).
* No database schemas — wire models are never ORM models.
* No HTTP client or server behaviour.
* No business validation beyond structural and range validation ("is this a well-formed result?",
  not "is this a *good* result?").
* No storage of payloads.

## 4. Responsibilities

| Responsibility | Detail |
|---|---|
| Payload models | Pydantic v2 models for every cross-application payload |
| Envelope | `schema`, `schema_version`, `generated_at`, `generator`, `payload` |
| Version negotiation | Accept unknown minors, reject unknown majors with a typed error, preserve unknown fields |
| Capability vocabulary | The canonical list, its version, and validation of IDs against it |
| Schema publication | Generated JSON Schema per payload version, shipped as package data |
| Golden payloads | At least three examples per payload version, shipped as package data for contract tests |
| Serialization | Canonical JSON output; `UNSUPPORTED ↔ "unsupported"`; RFC 3339 timestamps |

## 5. Dependencies

`baseaicore`, `pydantic>=2.9,<3`, `jinja2>=3.1,<4`. Nothing else.

Jinja2 arrives with `setspec.prompts` ([ADR-0028](../../adr/0028-prompt-pack-granularity.md)): prompt
records are versioned, hash-bearing documents whose hashes appear in cross-application evidence, so
they belong with the other contracts rather than being implemented three times. Consumers that want
schemas without a template engine are served by making it an extra if that need ever appears; today
all three applications need both.

## 6. Consumers

FreeWeight (producer of results/evidence), LoadCoach (consumer of evidence, producer of job events),
IdeaPress (consumer of results, producer of feedback), MirrorWall (event and error envelopes), and
any external tool reading a suite export.

## 7. Public API

```python
# Envelope and versioning
SchemaEnvelope[T](schema: str, schema_version: str, generated_at: datetime,
                  generator: GeneratorInfo, payload: T)
SchemaVersion(major: int, minor: int)                  # parse/compare/format
SUPPORTED_SCHEMAS: Mapping[str, Mapping[int, SchemaVersion]]
    # schema name -> {supported major: highest minor known}. Supported *majors*, not an exhaustive
    # list of versions: a newer minor within a supported major is accepted, so exact-version
    # matching would contradict the reader policy (ADR-0009 rule 3).
class SchemaVersionUnsupported(SuiteError): code = "SCHEMA_VERSION_UNSUPPORTED"

load_envelope(data: bytes | str | Mapping, *, expect: str,
              supported: Sequence[SchemaVersion] | None = None) -> SchemaEnvelope[Any]
dump_envelope(payload, *, schema: str, version: SchemaVersion,
              generator: GeneratorInfo) -> str        # canonical JSON

# Payload models  (each with a versioned module: setspec.benchmark.v1, …)
# Each payload type generates TWO model classes from one definition:
#   <Name>Out  -- extra="forbid",  used by writers
#   <Name>In   -- extra="allow",   used by readers; unknown keys round-trip through `.extras`
# A reader re-exporting data dumps through the In model, so an older reader never strips a newer
# writer's fields while writers still emit only known ones (ADR-0009 rule 4).

BenchmarkResult          # one benchmark, one measurement subject, metrics + provenance + samples ref
BenchmarkRunSummary      # one run: subject, suite, status, timings, aggregate metrics
EvidenceBundle           # many CapabilityEvidence + provenance, the FreeWeight → LoadCoach payload
CapabilityEvidence       # normative field set in ADR-0022: identity triple + canonical_id +
                         # identity_confidence, runtime_profile_hash, machine_fingerprint,
                         # capability_id, score, confidence, sample_count, excluded_count,
                         # dispersion, measured_at, computed_at, policy_version,
                         # vocabulary_version, benchmark_versions, dataset_hashes,
                         # prompt_subset_hashes, contributing_metrics, source_run_ids, environment
PromptRecord             # the prompt record schema (ADR-0028)
PromptManifest           # pack manifest, prompt_subset_hash helper
MachineProfilePayload    # exchange form of baseaicore.MachineProfile
ModelIdentityPayload     # exchange form of baseaicore.ModelIdentity + descriptor snapshot
EventEnvelope            # SSE and job/run event payloads
ErrorEnvelope            # the object inside {"error": {…}} — a shape, transported UNWRAPPED
                         # (ADR-0025 §4); never itself put in a SetSpec envelope
MetricValue              # value | "unsupported", unit, aggregation, higher_is_better, n, dispersion

# Vocabulary
CAPABILITY_VOCABULARY_VERSION: str
CAPABILITIES: frozenset[str]
validate_capability(capability_id: str) -> CapabilityId
is_known_capability(capability_id: str) -> bool

# Schema artefacts
json_schema_for(schema: str, version: SchemaVersion) -> dict
golden_payloads(schema: str, version: SchemaVersion) -> list[dict]

# Prompts (ADR-0028) — one implementation of a determinism contract, not three
class PromptLibrary:
    def get(self, prompt_id, *, version=None) -> PromptRecord: ...
    def render(self, prompt_id, variables, *, version=None) -> RenderedPrompt: ...
def prompt_subset_hash(records: Iterable[PromptRecord]) -> str
```

## 8. Inputs

JSON text, bytes or mappings from files and HTTP bodies; Python objects to be serialized.

## 9. Outputs

Validated model instances, canonical JSON strings, generated JSON Schema documents, typed version
errors.

## 10. Data ownership

Owns the **schemas**, not the data. It never persists anything.

## 11. Public contracts

1. Envelope shape is stable within a major version of each payload type.
2. Reader policy: unknown minor accepted (with unknown fields preserved), unknown major rejected with
   `SCHEMA_VERSION_UNSUPPORTED` naming both versions.
3. Round-trip fidelity: `load(dump(x)) == x` for every model class. For an `In` model this includes
   unknown-field preservation; for an `Out` model there are no unknown fields to preserve. The
   contract is asserted per class, never across the pair.
4. `dump_envelope` output is canonical JSON — byte-identical for equal inputs.
5. Measurements serialize as a number or the string `"unsupported"`, never `null`, never `0`.
6. Timestamps are RFC 3339 UTC with millisecond precision.
7. Every published version keeps its JSON Schema and golden payloads available for the lifetime of
   that major version.
7a. A prompt record's `sha256`, a rendering's `rendered_sha256` and a `prompt_subset_hash` are
   byte-stable across platforms, Python versions and installed `setspec` versions within a major —
   they appear in evidence that another application reads, so they are contract-grade, golden-tested
   determinism ([ADR-0028 §3](../../adr/0028-prompt-pack-granularity.md)).
8. The capability vocabulary is versioned: additions are minor, removals/redefinitions are major.

## 12. Configuration

None. Supported version sets are compile-time constants of the installed version.

## 13. Error behaviour

| Condition | Behaviour |
|---|---|
| Unknown/unsupported major | `SchemaVersionUnsupported` with `expected`, `received`, `supported` in `details` |
| Malformed JSON | `ValidationError` with the parse position |
| Structural violation | `ValidationError` listing every offending field path at once |
| Value out of range (score outside 0–1, confidence outside 0–1, negative sample count) | `ValidationError` naming field, value and bound |
| Unknown capability ID | `ValidationError` when strict; a preserved warning when the payload's vocabulary version is newer |
| Missing envelope fields | `ValidationError` naming them |

No silent coercion, no partial parse, no "best effort" mode.

## 14. Security considerations

* Payloads are untrusted input: parsing is size-limited by the caller, and models forbid unbounded
  nesting depth (a configured limit, tested with a deeply nested payload).
* No code execution, no dynamic imports, no `eval` — pydantic models only, never
  `parse_obj_as(Callable)`-style dynamic typing.
* Models never carry secrets; a test asserts no field name matches the secret pattern.
* JSON Schema documents are static package data, never fetched.

## 15. Performance

| Operation | Target |
|---|---|
| Validate a `BenchmarkResult` | ≤ 1 ms |
| Validate a 1 000-result `EvidenceBundle` | ≤ 500 ms |
| Serialize a 1 000-result bundle | ≤ 500 ms |
| Parse and validate a 50 MB export (streaming, JSONL) | ≤ 30 s, bounded memory |

Large exports use JSONL with one envelope per line so producers and consumers stream instead of
loading everything.

## 16. Cross-platform

Fully portable. Canonical JSON output must be byte-identical across platforms and Python versions —
asserted by golden tests in the CI matrix.

## 17. Observability

No logging. Errors carry structured `details` that consumers log with their request/run IDs.

## 18. Test strategy

| Area | Tests |
|---|---|
| Round-trip | Every model class, including optional and `unsupported` fields; an `In` model preserves unknown keys through load → dump, an `Out` model rejects them |
| Prompt determinism | Same record + same variables ⇒ identical bytes; the same record hashes identically under every `setspec` version in the compatibility matrix |
| Evidence field set | A `capability.evidence` payload missing `measured_at`, `policy_version` or `vocabulary_version` is rejected with the field named |
| Version negotiation | Same major/newer minor accepted; newer major rejected; older major rejected when unsupported; error names both versions |
| Unknown fields | Preserved through load → dump; a v1.0 reader re-exporting a v1.1 payload loses nothing |
| Validation | Out-of-range scores/confidence, negative counts, bad timestamps, bad capability IDs, missing required fields |
| Canonical output | Byte-identical repeats; key ordering; float formatting; non-ASCII |
| Golden payloads | Every golden validates; every golden is stable across releases |
| Schema generation | JSON Schema is generated, valid, and matches the committed snapshot (CI diff) |
| Vocabulary | Version bump rules; unknown capability handling in both strict and lenient modes |
| Streaming | JSONL reader handles a large file with bounded memory; a malformed line is reported with its line number without aborting the whole read (configurable) |
| Depth/size | Deeply nested payload rejected at the configured limit |

Coverage floor: **95 %**.

## 19. Compatibility and versioning

* Package version and schema versions are independent.
* A schema change without a version bump fails CI (snapshot diff).
* Coexisting majors live in versioned modules (`setspec.benchmark.v1`, `.v2`) and both are exported
  for at least one minor release of every consumer.
* The schema compatibility job validates every published version against its goldens on every CI run.

## 20. Acceptance criteria

1. FreeWeight can export an evidence bundle and LoadCoach can import it with **no** FreeWeight code
   or database access — demonstrated by a contract test that reads only the exported file.
2. A v1.1 payload is read by a v1.0 reader without loss; a v2.0 payload is rejected with both
   versions named.
3. JSON Schema is generated for every payload version and matches the committed snapshot.
4. Golden payloads exist (≥ 3 per version) and are used by contract tests in producing and consuming
   repositories.
5. `mypy --strict`, `ruff`, `lint-imports` clean; coverage ≥ 95 %.
6. Serialization is byte-identical across the CI matrix.

## 21. Future extensions

* ~~`production.feedback` payload~~ — **not created.** `POST /jobs/{id}/feedback` is an ordinary body
  of LoadCoach's own `/api/v1`, versioned by its path and contracted through the committed OpenAPI
  snapshot ([ADR-0025 §5](../../adr/0025-envelope-boundaries.md)). Recorded as a deliberate rejection
  rather than an oversight.
* `routing.decision` export payload for sharing routing explanations between tools.
* `benchmark.suite_manifest` as a shareable payload so suites can be exchanged.
* Optional compression for large bundles (gzip envelope) with the same schema semantics.
* Non-Python consumers via published JSON Schema (already possible; a generated TypeScript type
  package would be additive).
