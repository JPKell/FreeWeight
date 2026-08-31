# Changelog

All notable changes to `freeweight` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

- **CSRF double-submit on every HTML form route (ADR-0026 §2, Phase 14).** MirrorWall's
  `CsrfMiddleware` validates a `__Host-mw-csrf` cookie against a hidden `csrf_token` field;
  `web.csrf.CsrfCookieMiddleware` issues the token once per request and `render` injects it into
  every page, so a form only includes the `_csrf` partial and no route has to remember the token.
  Host validation is now outermost, so a DNS-rebinding form post is 421 before CSRF runs. The JSON
  API stays exempt on the stated content-type/CORS grounds.
- **Performance budgets: every spec §15 figure measured and asserted** (`tests/performance/`),
  with run start measured on a real socket (a `freeweight serve` subprocess), not only in-process.

### Added

- **External benchmark adapters (Phase 13).** Nine adapters — lm-evaluation-harness (MMLU-Pro,
  GSM8K), IFEval, EvalPlus, CRUXEval, BFCL, RULER, JudgeBench, LLMBar, CriticBench — run
  established external benchmarks as isolated subprocesses and normalize their output onto the
  same sample/metric shape native suites produce, each result carrying full provenance (source
  repository, pinned tag and commit, licence, dataset hashes) and the sandbox tier it used.
- **Tiered code-execution sandbox (ADR-0018): container (podman → docker) → bwrap → refuse.** The
  tier is decided once per run and recorded on every result; there is no host-execution tier and
  no fallback below the decided one. A code-execution benchmark on a machine with no tier is
  skipped with `sandbox_unavailable`, never run on the host — proven by an observer test and
  mutation-checked. One function (`run_sandboxed`) is the only door to sandboxed execution, held
  there by a structural test that no other module starts a subprocess.
- **Pinned datasets, verified before use.** A dataset is hashed before it is moved into place; a
  mismatch refuses and names both hashes. Archive extraction is hardened (no absolute paths, `..`,
  links, device files; per-entry, entry-count and decompression-ratio caps).
- `[sandbox]` and `[external]` configuration sections (previously spec-only), now in the generated
  configuration reference.
- `freeweight external list|install|verify` and a benchmark-source page crediting each project,
  its pinned version and commit, and its licence.

### Changed

- Adopted `weightsdb` (0.2.x) for all database plumbing: engine construction, session/transaction
  scopes, upsert, the migration runner, backup/restore and the typed error hierarchy. The
  in-application copies under `freeweight.infrastructure.db` (engine, session, upsert, migration,
  backup, errors — ~1,380 lines) are deleted; `types.py` remains as a re-export shim so existing
  migration revisions keep importing by their historical path. FreeWeight's models, declarative
  base and migration history are untouched: an existing 1.0.0rc1 database opens at head with no
  new revision, proven against a database file from a real rc1 install.

- Adopted `mirrorwall` (0.2.x) for the web shell: the base template, design tokens, layout and
  component CSS, theme/table/SSE/telemetry JS, the Jinja environment (`create_template_environment`,
  `StrictUndefined`, the shared filters) and the request-ID and Host-validation middleware all come
  from the package; FreeWeight keeps its own pages, navigation, and an `app.css` carrying only the
  rules that name this application's vocabulary (run-state colours, the event log, the comparison
  heatmap, the scatter and telemetry charts). The palette is byte-identical to the pre-adoption
  inline one; before/after full-page snapshots of every page differ only in live telemetry values,
  except the evidence page, where MirrorWall's `table.js` — now loaded on every page by the shared
  base — adds the column-visibility control and sortable headers to a table that previously had no
  script (an intended enhancement). The telemetry bar's element id changed to `mw-telemetry-bar`
  and localStorage column-preference keys to `mirrorwall-columns:*`, both extraction renames.
  `StrictUndefined` surfaced two latent template defects, both fixed: the run form's sticky values
  rendered blank on the plain page (`form_suite`/`form_model`/`form_label` had no defaults), and
  the goal wizard's rule step referenced `proposal.criterion` where the object's attribute is
  `criterion_key`, so its headings, anchors and hidden form value silently rendered empty.
- **`freeweight.services.prompts` now delegates to `setspec.prompts` (`setspec>=0.4,<0.5`).**
  The prompt record loader, validator, `StrictUndefined` renderer and both hash functions
  (`prompt_record_hash`, `prompt_subset_hash`/`pack_hash`) moved to `setspec.prompts` at SetSpec
  Phase 5 / LoadCoach Phase 4 (ADR-0011, ADR-0028) — this module is now a thin wrapper supplying
  only FreeWeight's own `PACK_ROOT` default. Every name this module exported before the move is
  still importable from here, unchanged, and the shipped pack hashes identically before and after
  the move (`pack_hash sha256:b1b0ffd0a5941fee5e0013d2a826732ea02a285b229bdc006ebd6dd25ff4ceb4`,
  golden-verified). Two of `tests/security/test_goal_pack_import.py`'s tests import the private
  sandboxed-environment constructor directly for a security assertion; those three import
  statements now point at `setspec.prompts` instead, which is the only source change outside
  `services/prompts.py` itself. The full test suite (2,297 tests) passes unchanged otherwise.

### Added
- **Capability evidence — the LoadCoach contract (Phase 11, M3).** Every completed run of a
  measurement subject — a model under a runtime profile on a machine — is folded into one
  `capability.evidence` record per capability, with ADR-0017's confidence beside the score, and the
  records leave the process only through SetSpec's own models: `GET /api/v1/evidence` returns a
  collection of `capability.evidence` envelopes, `GET /api/v1/evidence/export` one
  `benchmark.evidence_bundle`, and `freeweight evidence show|export` are the same two functions
  with a different front end. The M3 exit condition is a test: a `setspec`-only subprocess — no
  FreeWeight import, no database — reads the exported bundle and reconstructs a calibrated
  `user.*` record with its calibration block intact
  (`tests/contract/test_evidence_export.py::TestTheM3ExitCondition`).

  *Six factors, one formula, one owner.* `domain/confidence.py` is ADR-0017's `sample ×
  consistency × freshness × environment × identity`, times ADR-0032's `judge_validity_factor`,
  clamped to `[0.05, 1.0]` — each factor a pure function asserted against a hand-computed value,
  and the six recorded on every row so the UI explains a number rather than presenting it. Freshness
  decays from `measured_at`, the latest `completed_at` among the contributing runs, never from the
  aggregation time; recomputing unchanged runs lowers confidence or leaves it alone, and a test says
  so. Every parameter is configuration (`[evidence]`), recorded beside the `policy_version`, and a
  customised parameter or a custom weights file derives a *different* policy version from its
  content, so two policies coexist as two rows.

  *Weights are configuration.* `config/capability_weights.toml` is benchmark catalog §6 as a
  versioned file: for each capability, the shipped metrics that feed it and their weights, with a
  declared scale for every metric that has no natural `0..1`. A capability none of whose sources has
  a value is **absent** — never scored zero — and a suite that feeds nothing writes nothing.

  *A goal is emitted twice, or not at all.* A calibrated goal is emitted as `user.<slug>` with its
  `goal_hash`, `score_method_mix`, `judge_set`, `calibration` and validity factor, and — when it
  declares `contributes_to` — additionally as one weighted source inside that capability's record,
  which keeps the goal's identity too; never only as the shipped one. A goal below its calibration
  gate emits nothing for either, and the aggregation report says which and why rather than skipping
  silently. Hard separations partition: within a subject only the newest `(suite version, dataset
  hashes, prompt subset hash)` partition of a suite contributes, and the report names what was kept
  apart.

  *Evidence is recomputed, never edited.* A run's completion recomputes its subject's evidence;
  `freeweight evidence show --recompute` rewrites everything from the stored runs and lists every
  withholding. `capability_evidence` (migration `0007`) carries ADR-0022 §1's field set with
  `policy_version` in its unique key, `RESTRICT` on every identity key, and two internal columns the
  wire never sees: the policy parameters and the factor breakdown.

  *Staleness surfaces.* The `/evidence` page badges a record `stale` when its freshness has decayed
  below `evidence.stale_below` or its environment drifted, with the factors and the contributing
  metrics one interaction away; `freeweight health` gains an `evidence` component reporting the age
  of the newest evidence per capability.

- **The blinded grading screen for rung-4 (`human`) criteria.** `/runs/{id}/grade` presents a
  completed goal run's samples blinded — the model is never fetched — and shuffled, saving each
  grade on submit; `freeweight goals grade <slug> --run <id>` is the CLI form. A grade lands on the
  sample's own criterion row, the sample's composite is recomputed through the same function the
  run engine uses (`verdict_from_outcomes`, now public), the run's aggregate metrics are rewritten
  (`reaggregate_run`) and the subject's evidence is refreshed. A run whose goal has been edited
  since is refused: a grade against a different rubric would belong to a measurement it was never
  part of. Closes PHASE8_ISSUES §9.

- **The generated configuration reference.** `docs/configuration.md` is produced from the settings
  model by `scripts/generate_config_reference.py` — every field's key path, environment variable,
  type, default, range, runtime-changeability, security note and example — and CI's new `docs` job
  fails on drift (configuration standards §8). Every settings field gained a `description` and an
  `examples` entry to be the source of that document; a field without one fails generation.

- **The OpenAPI snapshot.** `docs/openapi.json`, produced by `scripts/generate_openapi_snapshot.py`
  and checked by the same `docs` job; the I3 milestone test no longer skips.

- **`[evidence]` configuration**: `n_target`, the two half-lives, `freshness_floor`,
  `stale_below`, `name_only_identity_factor`, the two drift factors, `goal_contribution_weight` and
  `capability_weights_path`. Spec §12 lists them.

- `freeweight version` and `GET /api/v1/version` report the schema versions this build writes
  (`schemas`), as CLI standards §2 asks; `freeweight --version` prints them inline.

- **The release workflow can publish, and can rehearse first.** `release.yml` gained the
  `publish-testpypi` job every package repository has — manual only, via *Actions → Release → Run
  workflow* — because packaging and release standards §6 requires a successful TestPyPI publish
  ahead of a distribution's **first** real release, and `freeweight` is an unclaimed name, so
  `v1.0.0rc1` is that release. The `release` job gained `environment: pypi`, which must match the
  Environment name configured on the PyPI trusted publisher; without it the OIDC exchange has
  nothing to match and the publish is rejected.
- **`requirements/release.in` and `release.lock`**, hash-pinned — required by packaging standards
  §4 and present in every package repository, absent here. `release.yml`'s two jobs and CI's
  `build` job install the locked chain and build with `--no-isolation`, so the wheel CI checks is
  produced by the same pinned backend as the wheel that ships; `pip-audit` audits the lock rather
  than the job's own environment. Verified by installing under `--require-hashes` into a clean
  3.13 interpreter, building, and running `twine check` on both artifacts.

  **`requirements/ci.lock` is deliberately not here yet.** `setspec>=0.3,<0.4` is not on PyPI, so
  `pip-compile` writes hashes for a locally built artifact that cannot match the wheel published
  from the tag — every `--require-hashes` install would fail on a mismatch. It lands in the commit
  after `setspec 0.3.0`; `requirements/README.md` carries the reason and the exact command,
  including the `--unsafe-package freeweight` this project's self-referencing `dev` extra needs.
- `jsonschema` and `types-jsonschema` in the `dev` extra: the producer validates what it emits
  against the *published* JSON Schema SetSpec ships, not only against the model that generated it
  (testing standards §8.2). Test only.

- **The nine endpoints spec §7.1 declared and no phase built.** `GET /api/v1/models`,
  `/models/{ref}`, `/models/{ref}/results`, `POST /models/discover`; `GET /api/v1/machines` and
  `/machines/{id}`; `GET /api/v1/benchmarks` and `/benchmarks/{key}`; and
  `POST /api/v1/runs/{id}/repeat`, which had a service function and a CLI command and no route at
  all.
- **`tests/contract/test_declared_surface.py` — and it came first.** It reads §7.1 out of the
  specification, not a list maintained beside it, and fails when a declared path is not routable.
  A path a later phase owns is named in its `SCHEDULED` map with the owner, so "not built yet"
  stays a decision rather than an absence.

  **It found three gaps the manual audit had missed** — the whole machines API and the repeat
  endpoint — which is the argument for asserting a specification against its build instead of
  reading both and comparing them by eye. Six paths had been declared and unbuilt since Phase 1.
- **`tests/integration/test_integration_milestones.py`** — roadmap §4's I1–I3 as tests a reviewer
  can point at (`pytest -m I2`). §4 says no integration milestone is complete "on the basis of a
  code review"; the verifications existed, spread across the import contracts, the telemetry tests
  and the export contract, and nothing labelled them.

### Changed
- **`setspec>=0.3,<0.4`.** The contract freeze, pinned in the release that ships the evidence
  export — exactly as the `0.2` pin's own comment promised.
- **FreeWeight is `1.0.0rc1`** (roadmap §6: `1.0-rc` at M3). The version file still said `0.1.0`
  through ten delivered phases; the M2 tag was never cut, so `0.9.0b0` is skipped rather than
  back-dated.
- **`tests/contract/test_declared_surface.py`'s `SCHEDULED` map is empty.** Both evidence paths
  are served, so their Phase 11 excuse is removed — the test fails if a served path stays excused.
- `PHASE8_ISSUES.md` §7 (the gate's conditional assertion) and §8 (`contributes_to` never emitted
  twice) are closed; the gate's absence is asserted directly, and end to end for both halves.
- The primary navigation gains **Evidence**, and a completed goal run's page links to its grading
  screen.
- **A metric's key is `metric_key` everywhere.** `GET /runs/{id}` and `freeweight run show --json`
  spelled it `key` while `/results`, the export and `/models/{ref}/results` spelled it
  `metric_key`; every shipped manifest, `MetricDefinition` and the generated goal manifests spelled
  it `key` again. One concept, two names, which is what CLAUDE.md's "same concept, same name across
  all nine repos" exists to prevent — and nothing caught it because no test compared two surfaces.

  Declaring a metric and reporting a value for one now name it identically, so a reader moving
  between a manifest and a result never has to translate: `MetricDefinition.metric_key`, all
  fifteen shipped manifests, the goal-suite manifest builder, both API surfaces and the stored
  `metric_definitions_json`. A contract test scans the Python source **and** every manifest and
  fails the build if either side drifts back.

  Every `manifest_hash` changes, which separates results from runs recorded before it — correct,
  and free here because no measurements are being retained pre-1.0.
- **A goal run generates every sample, then judges them all — the two never overlap.** Judging used
  to happen inside the per-sample loop, immediately after each generation, so with the provider's
  default `keep_alive` the candidate stayed resident while every juror loaded: a jury of three meant
  four models at once, on a machine chosen because it had room for one, and `2N` load cycles for `N`
  cases instead of `1 + jury_size`. The candidate is now evicted between the phases
  (`provider.unload`), so a juror has the machine to itself.

  **It costs the measurement nothing**, and that is asserted rather than assumed: a jury grades
  *stored text* — the collaborator's signature takes a `str` — so when it reads changes nothing
  about what it reads, and a test checks that the two-phase verdict is identical to the one-phase
  one, `result_json` included.

  Two things it fixes beyond memory. **Telemetry now describes the candidate**: the recording window
  closes and residency is observed before any juror loads, where previously a goal run's peak VRAM
  and energy total could belong to whichever model happened to be larger. And **a jury that fails
  takes one sample, not the run** — this phase runs after every answer exists, so aborting would
  discard a whole run's generation over one unjudgeable answer.

  A sample between the phases is `awaiting_judgement`: its own status, with no score and no
  `criterion_scores` rows, because half a criterion set is exactly the partial read those rows exist
  to prevent. It is the one non-terminal sample status, a completed run never contains one, and a
  run interrupted between phases resumes into judging without regenerating anything.
- **`benchmark.export` is now `freeweight.export`**, with no compatibility path for the old name
  ([ADR-0035](docs/adr/)). The schema sat in SetSpec's namespace and appeared in no
  `SUPPORTED_SCHEMAS`; a contract test now scans the source and fails on any schema name that is
  neither SetSpec's nor `freeweight.`-prefixed.
- **A run summary's metrics can be told apart.** `aggregate_metrics` carries `metric_key` from
  SetSpec, so an exported `benchmark.run_summary` is a list of *named* measurements rather than a
  list of numbers.
- **`native.energy` integrates only over the requests.** Each interval between two power readings
  is clipped to the union of the run's sample windows, so the settle wait, the warm-ups and the
  cooldowns no longer inflate every per-token figure. Clipping rather than filtering, because a
  reading taken inside a request whose next reading falls after it would otherwise carry the whole
  idle gap at the request's power level. `peak_gpu_power_watts` follows the same rule.
- **`native.reliability`'s questions live in `cases.json`** and are hashed into `dataset_hashes`,
  like every other suite whose content can drift. They were a tuple in a Python module, where only
  the suite *version* separated results — which depended on whoever edited them remembering to bump
  it (`PHASE9_ISSUES.md` §6, closed).
- **A suite declares its own `headline_metric`.** The dashboard's heatmap read a hand-maintained
  table in `services/results.py`, so a new suite was added in one place and forgotten in another.
  The declaration is now on the manifest, where the editorial judgement belongs
  (`PHASE10_ISSUES.md` §12, closed).
- **The scheduler re-reads stored settings between runs.** A run's effective configuration is frozen
  at creation, so re-reading between runs cannot change a measurement underneath itself — and it is
  what the settings page already promised. The telemetry sampler is deliberately not re-intervalled:
  its interval is recorded on every run as a measurement condition (`PHASE10_ISSUES.md` §11).
- **Wizard drafts have their own table.** `wizard_drafts`, with `created_at`, `updated_at` and an
  `expires_at` that every save pushes out, so the clock measures neglect rather than age. They were
  rows in `settings`, which could express none of that and made `db status` count half-written goals
  as settings (`PHASE10_ISSUES.md` §4, closed).

### Removed
- **`storage.retention_days`.** A measurement does not expire: a result taken six months ago is
  exactly as true as one taken today, and what invalidates it — the model leaving the machine, the
  hardware changing — is not something a clock can detect. The setting also applied to nothing,
  which read as a promise about disk usage the application was not keeping. Deleting a model's
  results is the operation that was actually wanted, and `scope=model` already previews and confirms
  like every other destructive one (`PHASE10_ISSUES.md` §9, closed).

### Added
- **`samples.started_at`.** A sample's telemetry window is now recorded at both ends rather than
  reconstructed as `created_at - client_wall_ms` — an approximation that could attribute a reading
  to the wrong request whenever the sampler interval was close to the request duration
  (`PHASE9_ISSUES.md` §3, closed; §4 closed behind it).
- **A context sweep on `results compare`.** A comparison of one model at three or more served
  contexts on one machine now carries the fitted KV cost function —
  `weights_bytes + bytes_per_token × context`, with `r²` beside it — differenced from each run's own
  `model_vram_bytes`. Derived rather than requested: a user who has run one model at several
  contexts has already produced the measurement. It is a *study across runs*, which is why it lives
  here and cannot be a suite ([ADR-0034](docs/adr/) §6). On the reference machine, three runs of
  qwen3:8b fit 4.64 GiB + 148 KiB/token at r² = 0.9998.
- **`benchmarks.long_context_max_tokens`.** The depth sweep's ladder is fitted to a configurable
  ceiling — truncated below it, doubling up to it — because how far a sweep can reach is a property
  of the machine, not of the suite. The *effective* ladder is hashed into that suite's
  `dataset_hashes`, so two ceilings are two measurements and are never averaged
  (`PHASE8_ISSUES.md` §18).
- **`--since` / `--until` on the results export**, half-open so consecutive windows tile: a history
  larger than one document exports as several that reassemble exactly, with each document stating
  the window it covers (`PHASE10_ISSUES.md` §8, closed).
- **`--include-prompt-text` on the results export**, adding an appendix of each distinct rendered
  prompt keyed by its hash. Built by re-rendering rather than by reading stored text — prompt text
  is not stored — so it also verifies: a prompt is only offered under the hash its current text
  produces, and one edited since the run is absent rather than present and wrong
  (`PHASE10_ISSUES.md` §10, closed).
- **[ADR-0034](docs/adr/) — run-level derived metrics.** The seam three suites use to compute
  figures no scorer can see — `native.memory_kv` needs the descriptor and the VRAM series,
  `native.energy` the power series, `native.reliability` every stored repetition — existed as an
  allowlist and a comment. It is now a decision: a pure `derive()` in the benchmark package, an
  explicit allowlist rather than a protocol every suite may implement, and the boundary that
  **a derived metric is a function of one run**. Benchmark catalogue §5.1 gains it as the fourth
  metric source (`PHASE9_ISSUES.md` §1, closed).
- **[ADR-0035](docs/adr/) — application-owned document schemas.** `benchmark.export` was minted by
  this application in SetSpec's namespace and appears in no `SUPPORTED_SCHEMAS`, which ADR-0025 §1
  says cannot happen. Applications now have a namespace of their own; the schema becomes
  `freeweight.export`, with the reader accepting both names until 1.0 (`PHASE10_ISSUES.md` §1,
  closed as a decision — **the rename is owed in code**).
- **`scripts/sync_docs.py`** — the `docs/` mirror is written rather than maintained, with `--check`
  for CI. The de-linking convention (a link out of the mirrored set is flattened to its text,
  because a link to a file this repository does not contain looks navigable and is not) is now the
  script instead of a habit. All seven documents, where there were four (`PHASE8_ISSUES.md` §6).

### Documentation
- **The eleven † metrics move to catalogue §3.15, out of 1.0 scope.** Seven audit figures, three
  long-context ones and the judge's repetition variance were specified before anything measured
  them and no phase ever took them. The section is titled "1.0 scope" and a metric with no owner
  does not belong in it; each is recorded with what it is blocked on, because what was considered
  and not built is worth as much to the next reader as what was (`PHASE8_ISSUES.md` §5, closed).
- **FreeWeight's version trajectory is written down: `0.9-beta` at M2.** The roadmap's §6 table had
  no application rows at all and started FreeWeight at M3, which left the version of a
  feature-complete application undecided and understated ten delivered phases. The tag is cut at
  M2 exit, not before — that exit is a demonstration on a real model, not a state of the source.
- **The generated configuration reference goes to Phase 11.** Configuration standards §8 requires
  one and it does not exist; spec §12 is hand-maintained and has drifted twice. The write-or-check
  pattern now exists in this repository, so the second generator is much cheaper than the first.
- **Spec §13: an uncalibrated judged goal runs rather than refusing.** The spec and
  [ADR-0032](docs/adr/) §3 contradicted each other and the code followed the ADR. The ADR's
  argument — the diagnostic data is what the user needs to fix the rubric, and it costs one
  GPU-bound run — applies *more* strongly before the first calibration than after a failed gate:
  the author has nothing at all to look at (`PHASE8_ISSUES.md` §12, closed).
- **Prompt standards §6 marks an override at run granularity.** "Every record that used them" read
  as a column on `samples`, which would store one identical value ten thousand times and offer no
  fact the run does not already carry (`PHASE8_ISSUES.md` §14, closed).
- **The rung-4 grading UI belongs to Phase 11.** Phase 10 was named as its owner and shipped the
  *calibration* half only, which is how it came to belong to no phase. Phase 11 is where a human
  grade first has somewhere to go (`PHASE8_ISSUES.md` §9, closed).
- **Spec §13 lists five error codes it was missing** — `GOAL_PATH_UNSAFE`, `GOAL_HASH_MISMATCH`,
  `PROMPT_OVERRIDE_REFUSED`, and `COMPARISON_SUBJECT_NOT_FOUND` / `COMPARISON_REFUSED`, found while
  writing the first three — each with why the shared set could not describe it. **API §11** is the
  code-to-status table the document never had, and **API §12** names every schema an export emits.
- **Spec §12 documents `[runtime]`**, which shipped in Phase 10 and was undocumented: what it sends,
  that every field separates results, why the provider's own default can be catastrophic on a
  memory-constrained machine, and why the fields Ollama configures at server startup are
  deliberately absent.
- **Spec §7.1 and §7.2 mark every declared-but-unimplemented surface**, six paths and four command
  groups, and name what would have caught them: §7.1 has no test asserting its own paths are
  routable.
- **Spec §14 states the regex guard correctly.** The dialect refuses a catastrophic pattern at
  pack-load time; the timeout is the backstop. The previous wording promised the timeout would do
  work CPython cannot let it do — the GIL is held for the whole match, measured at 3.1 s against a
  50 ms budget.
- **UI/UX standards §1 carries the compliant palette**, with measured contrast for both themes
  against both grounds and the bar each token must clear. The standard's own `--mw-text-subtle` and
  `--mw-accent` could not satisfy its own §13, so §13 asked for something no build could pass; it
  now asks for the pairs the application renders, each against its role.
- **Benchmark catalogue**: `source` on a metric definition is documented; §5.1 explains why the
  fallthrough is unsafe for a conditional rate; a † marks every metric declared and owned by no
  phase; §5 states that a suite's headline metric belongs on its manifest.
- **Subjective Goals §2.3** describes the goal-pack bundle — the format `goals import` actually
  reads, as distinct from the `benchmark.goal_pack` envelope that describes a pack and cannot
  rebuild one. **§8**'s starter table is the reading order, four packs and four figures.
- **Honest statements of what does not happen**, each where a reader would otherwise assume
  otherwise: retention deletes nothing on a timer (spec §12), `include_prompts` exports identity
  rather than text (API §5), `PUT /settings` does not re-interval a running sampler (API §8), wizard
  drafts live in the `settings` table and should not (data model), rung-4 criteria are skipped and
  the run-grading UI belongs to no phase (Subjective Goals §3.3).
- **Packaging standards §4's dependency example is dated.** The block is the state as of M3; copied
  verbatim before then it made this distribution unresolvable, which is what happened.
- **`max_context_capped_by_configuration`** beside `max_successful_context_tokens`. The two cases
  report the same number and are not the same fact — "the model refused at the next rung" is the
  model's limit, "the sweep stopped where it was told to" is the configuration's — and a reader
  comparing two models could not previously tell them apart (`PHASE9_ISSUES.md` §7, closed).
- **`served_context_observed`** on every run, from the provider's own report of what it is serving.
  Where it contradicts a context the run merely *assumed*, the run carries a
  `served_context_assumed_incorrectly` degradation naming both numbers. The frozen fingerprint is
  never rewritten: provenance that changes after the fact is not provenance.
- **`ResidentModel.context_length`** in ModelRack, parsed from the `context_length` that `/api/ps`
  reports beside `size_vram` and was being discarded. It is what makes a *reported* served context
  distinguishable from an *assumed* one at all (ADR-0023 §4).

### Fixed
- **Every mock-tool search raised `UnicodeDecodeError` once FreeWeight was installed rather than
  run from a checkout.** `pip` byte-compiles every `.py` file in the wheel, and the fixture
  repository under `benchmarks/fixtures/data/repo` is `.py` files that are *content* — a small
  repository for a model to explore — rather than code. An installed copy therefore grows
  `__pycache__` directories no source checkout has, and `search_text`/`search_symbol` read every
  file under the root as UTF-8, so the first `.pyc` ended the benchmark case with an exception
  instead of answering it. The caches were visible in `list_directory` too, which meant the
  repository a model explored depended on how FreeWeight had been installed — the one thing a
  fixture exists to hold still. They are now excluded wherever the repository is enumerated, and a
  file that is not UTF-8 text is a refusal rather than a traceback, per this module's rule that a
  failed tool call is a value the model can act on. Only this fixture tree was exposed: every
  other package-data walk globs `*.json`, and a starter goal pack is a directory of JSON with no
  `.py` beside it. Found by the release workflow, which is the only job that tests the built
  wheel.
- **A corrupt SQLite database made `integrity_check()` raise instead of report, and a restore
  that landed a bad file was left in place because of it.** SQLite answers the same corruption two
  ways — a row naming the damaged pages, or the statement failing outright with "database disk
  image is malformed" — and which one it picks varies with the damage and the SQLite build. Only
  the first was handled, so on the other half `restore()` propagated a raw SQLAlchemy error past
  its own rollback: the corrupt file stayed as the live database, the `.pre-restore` copy was
  orphaned beside it, and `db status` reported "could not open the database" rather than an
  integrity failure. Both shapes are now the same `ok=False` result carrying the driver's message,
  and `restore()` puts the original back either way. A corrupt backup is likewise refused with one
  wording ("failed its integrity check") whichever shape SQLite reports. Found by CI, where the
  version-dependent branch was the one that ran.
- **Seven run-level metrics were emitted and declared nowhere.** `model_vram_bytes`,
  `model_total_bytes`, `served_context_observed` and the four telemetry figures go into
  `metric_values` on every run, and no manifest names them — so a consumer reading that table could
  not tell what they were or whether to expect them, and the suite-conformance tests were right to
  refuse them. They are now declared once, in `RUN_PROVENANCE_METRICS`, which is also where the
  emitters read their unit and direction from; the tests allow exactly that set beyond a manifest
  and nothing else. Found by the live suite, which is the only place all seven appear together.
- **The bare `pytest` entry point could not collect five test modules**, and it is the one CI runs.
  Under the default `prepend` import mode pytest puts the *test file's* directory on `sys.path`, not
  the repository root, so the five modules that import shared fixtures as `tests.conftest` resolved
  only under `python -m pytest`, which inserts the working directory. `pythonpath = ["."]` makes
  both invocations agree. Found by running the documented gate command literally.
- **The jury was served at the provider's choice too.** `JuryService` built its own
  `GenerationRequest` without a runtime profile — and the model that took a machine down was the
  *juror*, not the candidate. A goal run's jury now serves under the candidate's own profile, since
  judging at a different context than the answers were generated at would be a second, unrecorded
  variable; both calibration juries serve under `[runtime]`.

### Changed
- The live suite's model guard derives its budget from the machine's actual VRAM — card, less a
  reserve for whatever else draws the display, less the KV allowance at the pinned context —
  instead of a hard-coded constant. On the reference machine that took the usable model count from
  1 of 11, to 6, to **9 of 11**, excluding only the two that genuinely do not fit.

### Added
- **A run can be served at a chosen context.** `[runtime] context_size` in configuration,
  `freeweight run start --context-size`, and a `runtime` block on `POST /api/v1/runs` — the
  section and the flag ADR-0023 §1 and §3 named and nothing implemented. Measuring one model at
  8K and again at 64K is now two subjects with two runtime profile hashes and two fingerprints,
  which is what ADR-0017's hard separation requires and what a context comparison needs.
- **Per-model memory is recorded on every run** as `model_vram_bytes` and `model_total_bytes`,
  read from the provider's own residency report rather than from device-wide telemetry. Paired
  with the run's `served_context` this gives the allocation function a scheduler needs — two runs
  at two contexts yield the bytes-per-token slope exactly, with no regression against a device
  total that other processes also move. Emitted only when the provider actually reports residency;
  a provider that cannot produces no row rather than a fabricated one.

### Fixed
- **The runtime profile was never sent to the provider.** `_build_request` built every
  `GenerationRequest` without one, so a run's profile was stored, hashed into the reproducibility
  fingerprint, and then not used — every run was served at whatever the provider chose while its
  record named a profile it had never been asked for. With a modern local model advertising
  128K–262K context, that meant enormous KV allocations and performance numbers dominated by spill:
  on one machine a 15.7B model was served at 112K, spilled 21.9 GiB of KV cache to system RAM, ran
  at 3.9 tokens/second, and took the display driver and the kernel down with it. The profile now
  reaches the provider, warm-up warms under the same profile it measures under, and `repeat_run`
  repeats the original's stored profile rather than re-resolving from current configuration.
- `resolve_served_context`'s `CONFIGURED` branch was unreachable, so every run recorded the model's
  *advertised* maximum as its served context with source `assumed`. Runs that set a context now
  record `configured` and a number that is a fact.

### Added
- **A nightly workflow** (`.github/workflows/nightly.yml`) running `-m live` and `-m performance`.
  Testing standards §10 schedules both nightly on hardware and neither ran anywhere; the
  performance suite added in Phase 10 in particular had no home. The live job reports its skips
  with `-rs`, so "no Ollama on this runner" cannot be mistaken for a pass.
- **`diff-cover` as a pull-request gate.** Testing standards §10 lists it among the gates required
  before merge. A repository-wide coverage floor says nothing about whether *this change* was
  tested, and a large well-covered codebase absorbs an untested addition without the floor moving.
- **Contract tests for the three schemas FreeWeight emits** (`tests/contract/test_export_schemas.py`):
  `benchmark.export`, `benchmark.goal_pack` and `benchmark.calibration_report`, each written
  through its strict outbound model and read back through the preserving inbound one, plus the
  unknown-field round trip API standards §7 rule 4 requires of a reader. The `contracts` CI job
  previously selected zero tests and exited 5 — it failed on every push.
- **Live coverage of the M2 exit condition** (`tests/live/test_real_run.py`): the Phase 9 suites,
  Phase 10's drill-down/comparison/export on real runs, and a subjective goal authored through the
  wizard's own forms, calibrated against a **real jury**, and scored — on real weights. The
  grades are supplied from a fixed pattern rather than by a person, which the test says outright:
  it demonstrates the pipeline, not that a human would agree with the jury.
- The three FreeWeight documents absent from this repository's `docs/` mirror —
  `benchmark-catalog.md`, `data-model.md` and `risks.md`. Their absence had also forced links to
  them out of the four documents that were mirrored; those links are restored.

### Changed
- **Dependency ranges corrected to what exists and what is verified.** `setspec>=0.3,<0.4` was
  copied from the packaging standards' example, which describes the post-M3 state: SetSpec is 0.2
  until its schemas are frozen at M3, so the declared range made this distribution unresolvable.
  It is now `>=0.2,<0.3` and moves with the freeze. Four of the seven `dev` pins were a major
  behind the toolchain the gate is actually run with (pytest, pytest-cov, pytest-randomly, mypy),
  so CI installed a different toolchain than any developer ran.
- The end-to-end fixtures disable the idle-detection wait. The shipped default waits for three
  consecutive quiet telemetry observations at one-second intervals before the first provider call,
  which was **2.2 s of every run** in the suite and the same wait on every one of them. Idle
  detection keeps its own tests, which cover all three of its outcomes. The default suite went from
  4:55 back to **2:15**, under testing standards §10's three-minute limit and below the
  pre-Phase-10 baseline.

### Added
- **Phase 10: the dashboard, the results experience, data management and exports.** A dashboard
  that answers the four questions before the user scrolls, and answers them with *one stored
  measurement from one named run* — the latest completed run of that suite for that model. Nothing
  on it is averaged across runs, because averaging two runs that measured different benchmark
  versions on different hardware produces a number about nothing; the anti-lie test recomputes
  every headline figure straight from the raw samples and requires equality. Every figure is at
  most two interactions from the records behind it, and the last of those is a **case inspector**
  showing one request's prompt identity, response, tool calls, per-criterion scoring, juror
  rationales and the telemetry observed while it ran.
- **A metric-level results query** (`GET /api/v1/results`, `freeweight results list`) with cursor
  pagination over a total order — run creation, run ID, metric key and the metric row's own ID,
  because one run legitimately holds several rows under one key and a shorter sort key silently
  skipped the second of every pair.
- **Streaming exports** in JSON, JSONL and CSV over run, model, suite, comparison and whole-database
  scopes (`GET /api/v1/results/export`, `freeweight results export`). The document leaves the
  process as chunks, so a 10 000-sample run costs one run of memory rather than all of it, and the
  assembled JSON is byte-identical to the canonicalizer it claims to match. Unavailable
  measurements are the string `"unsupported"` in every format — never `0`, never `null`, never an
  empty cell.
- **Data management that previews before it removes anything.** `GET /api/v1/database/stats`,
  `POST /api/v1/database/delete-preview`, `DELETE /api/v1/database/results`, and the page behind
  them. The preview returns a token computed over the selection *and* the counts; the deletion
  recomputes both and refuses if either moved, so "this will delete 412 rows" is a statement rather
  than a hope. Models, machines, descriptors, runtime profiles, benchmark definitions, goals and
  every calibration row are untouched and are re-counted afterwards to show it.
- **A settings page for the settings a running server may change**, by allowlist. Anything
  security-relevant — the bind address, the exposure flag, auth tokens, the remote-provider
  allowance, the database URL, the data roots — is refused with `403 FORBIDDEN` naming the key, and
  is listed on the page as read-only with its environment variable beside it.
- **`freeweight results list|show|export`**, completing spec §7.2's four verbs, and a realistic
  `Example:` in every command's help across the whole CLI.
- **Phase 10A: the goal authoring wizard and the four starter packs.** Seven server-rendered steps
  that take a user with no prior setup from a sentence to a JSON goal pack they own. Step 2 is the
  part that earns the feature: of every criterion it asks whether two people would grade the same
  text the same way and whether it is one quality or two, and where the answer is "two" it makes
  the split visible rather than performing it. The rule proposer never applies a rule — accepting
  one is the only thing that moves a criterion down the ladder, and the running statement of how
  much weight has come off the judge updates as it happens.
- **Blinded, resumable inline grading.** The model that produced a sample is never fetched, the
  order is a stable per-goal shuffle, and every grade is a stored row rather than page state — so a
  refresh, a server restart mid-sitting and an out-of-order regrade all lose nothing and duplicate
  nothing. The calibration report states the band and its consequence in words, with the
  coefficient and `n_holdout` beside them, and shows the samples where the jury and the author
  diverged most.
- **Four starter packs** — `creative_voice`, `technical_explanation`, `brand_voice` and
  `summary_faithfulness` — each with tasks, criteria, proposed rules and a worked, graded
  calibration set that reproduces its documented agreement figures under a fixed seed and a
  reference jury. Read in that order their deterministic weight rises 40 % → 55 % → 70 % → 90 %.
  A fork that has not been edited is badged `unforked` in the pack, in the UI, in its results and
  in its exports.

### Changed
- `db status` now reports row counts for **every** table the application owns. The previous set
  omitted `runs`, `samples` and `metric_values` — every table that actually holds a user's
  measurement history, and every table a deletion touches.
- `GET /results/compare?subjects=…&suite=…` accepts **model** references as well as run
  references: a model resolves to its latest completed run of `suite`, and naming a model with no
  suite is refused rather than guessed. The run reading and the `suite` guard are unchanged. This
  closes `PHASE9_ISSUES.md` §9; the resolver lives beside the dashboard's so the two cannot come to
  disagree about what "latest" means.
- The design tokens gained `--mw-accent-text` and `--mw-border-strong`. The brand accent is
  3.87:1 on white, below the 4.5:1 UI/UX standards §7 calls non-negotiable for body text, so link
  text uses a darkened pair while the brand blue keeps filling buttons, drawing the focus ring and
  colouring the chart series. A contrast test now runs over every token pair the application
  actually renders, in both themes.

### Fixed
- Cursor pagination truncated its timestamp to milliseconds, so the second page of any result set
  came back empty. Cursors now carry the stored instant at full precision.

### Added
- **Phase 9: memory, energy, reliability, and a comparison engine that refuses to average.**
  `native.memory_kv` computes a theoretical KV cost from the descriptor's own architecture fields
  and compares it against the VRAM slope actually observed across a 1K–64K context sweep, reporting
  the fit quality beside the slope so the phase's named risk — slope noise from another process —
  is visible rather than averaged in. A missing architecture field produces `unsupported`, never a
  number built from a guess; a hybrid or state-space architecture is flagged and excluded from the
  transformer formula rather than forced through it; and an out-of-memory rejection is stored as
  the maximum-context measurement it is, not as a failed run. The cache-reuse pair is two tests in
  two measurement classes, so a first pass over a prefix and a reuse of it can never be averaged
  into one prefill number.
- `native.energy`: joules integrated from the power samples' **own timestamps**, never from the
  sampler's nominal interval — which drifts most exactly while the device is busiest — with joules
  per request, per output token and per successful task, tokens per joule, successful tasks per
  kWh, peak power, host temperature and the throttle verdict. Every figure is labelled a
  telemetry-derived estimate by the type that carries it, and an unknown throttle state is
  `unsupported` rather than "did not throttle".
- `native.reliability`: dispersion, the unbiased `pass@k` over every stored repetition, and
  byte-level answer agreement across repetitions. Outliers are flagged and reported under a named
  policy and are never silently discarded; where a caller chooses to exclude, the policy, the
  threshold and the removed values come back with the result.
- `freeweight.domain.statistics`: mean, median, min, max, sample standard deviation, coefficient of
  variation and percentiles, `pass@k` and answer agreement — each returned as a `Statistic` that
  carries the sample count it used and the count it excluded, so a figure cannot be reported
  without them. An all-`unsupported` series is `unsupported` with a reason, never `0`, and a single
  observation has no standard deviation rather than one of `0.0`.
- `freeweight.domain.comparison`: comparability verdicts over BaseAiCore's matrix, plus the two
  facts a measurement subject cannot carry — the descriptor's family and quantization — which turn
  its `indeterminate` into a named quantization study. Subjects are partitioned per metric kind
  into groups that may be merged, and a subject joins a group only when it clears *every* member of
  it. Every separation carries the field-level fingerprint diff that explains it.
- `GET /api/v1/results/compare`, the `/compare` page and `freeweight results compare`: one row per
  metric, one column per run, the sample and exclusion counts under every figure, and a visible,
  labelled separation wherever two columns must not be read against each other. Comparing runs from
  two suite versions is refused with the versions named and the diff printed; the CLI still exits
  `0`, because "these cannot be merged, and here is why" is an answer rather than a breakage.
- **Phase 8: four judgement-dependent suites, and judge infrastructure that measures the judge.**
  `native.audit` runs a mutation corpus *and its clean originals*, so "a model that reports many
  possible problems must not score well" is a measurement rather than an aspiration: precision,
  recall and F1 sit beside a clean-code false-positive rate, and localization is scored apart from
  detection. `native.critique` reports correction uplift and — as a headline, not a footnote — the
  regression rate, measured on the half of the corpus whose answers were already right.
  `native.judge` measures a model *as* a judge across all seven of the catalog's tests: pairwise
  correctness, position bias, repetition stability, verbosity bias, style bias, transitivity and
  self-preference, every figure produced by counting verdicts rather than reading them.
  `native.long_context` sweeps depth, needle position and distractor volume and reports
  `effective_context_tokens` against a recorded threshold — a number that says nothing at all when
  a model fails everywhere, rather than reporting its shortest tested context — beside
  `longest_tested_context_tokens`, so a model that did not fail anywhere the sweep looked reads as
  a floor rather than as observed degradation.
- `freeweight.domain.judging`: the selection, blinding, order randomization, repetition, agreement
  and linkage every judged number stands on, in one place, so that `native.judge` and the goal
  suites cannot do any of them differently. A juror never judges its own output, and the refusal is
  recorded rather than discounted.
- **Phase 8A: goal packs.** A user can hand-write a goal whose criteria are entirely rules, run it
  against a model, and get a scored, drillable result with **no judge involved anywhere**. Thirteen
  rung-2 rule types and four rung-3 reference types, each a pure function whose docstring states
  what it refuses; a composite with hard gates, weights and `score_method_mix`; `goal_hash` over the
  measurement-defining subset only, so renaming a criterion keeps a year of results together and
  changing what it checks separates them.
- `goals`, `goal_criteria`, `goal_tasks` and `criterion_scores` (migration `0005`), with
  `criterion_scores` written in the same transaction as its sample so a composite can never be read
  back with fewer criteria than the sample it belongs to. A skipped criterion has `raw_score = NULL`
  and a check constraint says so.
- `freeweight goals list|show|init|edit|validate|suggest-rules|export|import` and
  `GET|POST|PUT|DELETE /api/v1/goals` with `/validate`, `/suggest-rules`, `/tasks`, `/export` and
  `POST /goals/import`. `PUT` reports the old and new `goal_hash` and how many existing runs the
  change would separate **before** it is applied; `DELETE` previews what it would orphan and how
  many of the author's own grades it would destroy.
- The rubric lint: it flags a judged criterion a rule could check and names the rule, refuses a
  judged criterion whose scale carries no descriptors, and reports every problem a pack has at once
  with a severity each. It never rewrites the author's criterion.
- **Prompt overrides, wired** (prompt standards §6). `$XDG_CONFIG_HOME/freeweight/prompts/` is
  loaded at startup; a benchmark run that would render an overridden prompt is **refused** unless
  `--allow-prompt-override` is passed, and when it is, the override becomes a
  reproducibility-fingerprint input and a recorded degradation — so the results separate rather
  than silently merging with runs of the shipped prompt.
- **Phase 8B: calibration, the jury and the gate.** A judged criterion becomes a *measurement*: the
  author grades, a seeded stratified partition splits the grades into anchors and a holdout, the
  jury scores the holdout it has never seen, and the agreement is reported with every number.
  Quadratic-weighted Cohen's kappa, Spearman's rho, mean absolute error and signed bias, none of
  them ever separated from the `n_holdout` they were computed over; Krippendorff's alpha across
  jurors; and `judge_validity_factor` with ADR-0032 §2's shrinkage, so six held-out samples at
  `kappa_w` 0.71 yield 0.55 and not 0.71.
- `calibration_samples`, `calibration_grades`, `calibration_reports` and `judge_verdicts`
  (migration `0006`). Grading is resumable and idempotent per `(sample, criterion)`, because
  grading twelve samples across five criteria is a real sitting; `judge_verdicts` keeps one row per
  juror per repetition in full, because the jury's dispersion *is* the measurement's error bar.
- `freeweight goals calibrate|grade|calibration show|report`, `freeweight judges list|validate`,
  and the calibration and judge endpoints. A goal below the agreement gate is a `200`, not an
  error: the run completes, every sample is inspectable, the result is badged `uncalibrated`, and
  the diagnostics name the criteria and the specific held-out samples where the jury diverged from
  the author — with both rationales. `CALIBRATION_INSUFFICIENT` is a different state with a
  different remedy, and the code, the API and the copy all keep them apart.

### Changed
- The Jinja2 environment prompts render in is now sandboxed
  (`jinja2.sandbox.SandboxedEnvironment`). `StrictUndefined` and a missing loader do not stop
  `{{ ''.__class__.__mro__ }}` from resolving, and a goal pack imported from another machine is
  somebody else's file — spec §14 calls it untrusted input to FreeWeight's own renderer, and this
  is what that requires.
- The user-supplied regex dialect now refuses **unbounded repetition of a group** —
  `(?:a|a?)+`, `(a+)+b`, `(a|ab)*c` — at pack-load time. That is where catastrophic backtracking
  lives, and CPython's regex engine holds the GIL for the whole match, so no in-process timeout can
  interrupt one once it starts. Bounded repetition and every character-class quantifier are
  unaffected. See `PHASE8_ISSUES.md` §11.
- A benchmark manifest's metric may declare `source` (`auto` | `detail` | `score`). `auto` is the
  default and is the existing three-source resolution order; `detail` removes its final fallback,
  so a conditional rate whose denominator was empty for *every* sample is reported as unmeasured
  rather than silently becoming the mean score under that metric's name.
- A goal suite installs as `<goal_pack_version>+<first 8 hex of goal_hash>`, so a measurement-
  defining edit cannot land in the previous version's series even when the author did not bump the
  pack version.

### Fixed
- Nothing: these phases add behaviour rather than correcting it. The two defects found while
  building them — an unpartitioned calibration sample being renderable as a judge-prompt exemplar,
  and a goal slug reaching `mkdtemp` before it was pattern-checked — were introduced and fixed
  within the same phase, and both are asserted against in
  `tests/integration/test_calibration_flow.py` and `tests/security/test_goal_pack_import.py`.

### Added
- Phase 7: five deterministic quality suites. `native.instruction_following` checks
  machine-checkable constraints across format, length, keyword, structure and language classes and
  reports strict, loose and instruction-level accuracy separately; `native.structured_output`
  measures JSON validity and JSON-Schema conformance with one corrective retry, reporting the
  first-attempt rate and the recovery rate as distinct figures; `native.tool_use` covers the
  catalog's eleven tool scenarios; `native.tool_recovery` injects six deliberate tool failures;
  and `native.agent` scores four multi-step goals on their trajectory as well as their answer.
  **No model scores anything in any of them.**
- A mock tool harness over shipped fixture data (`freeweight.benchmarks.fixtures`): ten tools —
  calculator, `read_file`, `list_directory`, `search_text`, `search_symbol`, `lookup_record`,
  `database_query`, `get_inventory`, `write_sandbox_file`, `run_mock_test` — with no shell, no
  unrestricted filesystem, no network and no real database (spec §14). Reads are contained to the
  fixture repository and writes to a run-scoped sandbox by `contained_path`, which resolves
  symlinks before deciding; arguments are validated against each tool's own schema before it runs;
  and the calculator parses arithmetic with its own recursive-descent parser rather than any form
  of `eval`.
- Six scorers, all on rung 2 of the scoring ladder: exact match with declared normalization,
  constraint checking, a bounded JSON-Schema validator that **refuses** a keyword it does not
  decide rather than reporting conformance it never checked, tool selection, tool arguments, and
  agent trajectory.
- `freeweight prompts list|show|build` (prompt standards §3). `build` regenerates the pack
  manifest; `build --check` reports drift and exits 5 without writing, so a prompt record edited
  without a regenerated manifest fails CI instead of shipping hashes that describe prompts nobody
  installed.
- Benchmark prompt records for the five suites, each declared in its suite's manifest so its
  `prompt_subset_hash` covers exactly the prompts that suite renders.
- The `tool_calls` table (migration `0004`), specified in the data model since the freeze and
  unreachable until there were suites that call tools. One row per invocation a model requested,
  written in the same transaction as its sample and cascade-deleted with it, so a tool metric
  drills to the call that went wrong rather than to a rate. A call naming a tool that was never
  offered is a row with `status = "unknown_tool"`, not a missing one; `correct_tool` and
  `correct_arguments` are `NULL` — never `false` — where the case declares nothing to compare
  against. Tool results are stored as a hash and a short digest, never in full.
- Phase 6: the first real measurements. `native.performance` measures prompt-evaluation and
  decode throughput at the catalog's prompt sizes and output lengths, time to first token,
  streamed inter-chunk latency and cold model load; `native.token_economy` measures what an
  answer costs in tokens, characters, words and bytes, with the four derived per-success figures.
  Cold and warm results are separated rather than averaged, and a per-token latency figure appears
  only where the provider reports one token per streamed delta.
- The prompt library (`freeweight.services.prompts`): versioned JSON prompt records, a pack
  manifest, a `StrictUndefined` renderer, canonical record hashing and `prompt_subset_hash`, with
  the whole pack loaded and validated once at startup. The **per-benchmark subset hash** — not the
  pack hash — is the reproducibility-fingerprint input, so editing a prompt no benchmark uses
  separates no results (ADR-0028). Written as an in-application module that becomes
  `setspec.prompts` at LoadCoach P4.
- The reproducibility fingerprint of Machine Identity §4, assembled in
  `freeweight.domain.provenance`: model identity and digest, runtime profile, provider and
  version, machine, the drift-sensitive environment, the benchmark's manifest hash and prompt
  subset hash, the resolved execution parameters, the served context with its source, and the
  target GPU index. The full document is stored, not only its hash, and two documents diff field
  by field.
- `freeweight run repeat <run>` re-executes a recorded run with its identical frozen
  configuration; `--check` prints the field-level provenance diff afterwards. It refuses, naming
  every field that moved, when the model digest, the machine, the provider version or a dataset
  hash has changed, and `--force` proceeds and records the divergence on the new run.
- Telemetry persisted for the duration of a run, split across two tables and migration `0003`:
  `telemetry_samples` holds one row per observation with the host fields and
  `telemetry_gpu_samples` one row per visible device, so no host field is double-counted on a
  multi-GPU machine (ADR-0027). Every derived figure names its device, and memory, power, energy
  and temperature are `unsupported` with `multi_gpu_placement_unknown` when more than one GPU is
  visible and the provider does not report placement.
- The telemetry-sampling overhead is measured on the machine before each run and stored on the
  run (`runs.telemetry_overhead_percent`), so the distortion is provenance rather than an
  assumption (spec §15).
- Idle detection with a defined outcome (spec §13): the run waits for GPU and CPU to fall below
  `execution.idle_gpu_threshold_percent`; `on_idle_timeout = "warn"` proceeds and records a
  `measured_while_busy` degradation with the observed utilization, and `"refuse"` fails the run
  with `INSUFFICIENT_RESOURCES` and the same numbers.
- The served context is resolved and recorded with its source (`configured` | `reported` |
  `assumed`), and a benchmark case that needs more context than the model is served is stored as
  a `skipped` sample with its reason rather than sent and failed.
- The run detail page shows the provenance table, the stored fingerprint document, per-device
  telemetry charts (server-rendered inline SVG with a text summary beside each), metric spread and
  device attribution; the sample drill-down names the prompt record and version behind every
  sample and shows its time to first token.
- `execution.cooldown_seconds` is now honoured between tests, and `execution.gpu_index` and the
  idle settings are frozen into every run's effective configuration.

### Changed
- A benchmark test's `requires.provider_capabilities` is now **enforced**. A provider that has not
  declared a capability a test needs makes that test `skipped` with
  `run_tests.skip_reason = "unsupported_capability"` and `CAPABILITY_UNSUPPORTED` on the row; the
  test produces no samples and contributes no score, and the run completes normally. A model
  without tool calling therefore yields a recorded skip, never a low score (spec §13, graceful
  degradation).
- The run engine can execute a benchmark test through a declared *interaction* — a bounded tool
  loop, or a call plus one corrective retry — instead of a single provider call. Token counts are
  summed across an interaction's turns, the whole trajectory is stored on the sample, and a
  trajectory is scored by a trajectory scorer rather than on its final sentence.
- Aggregation now reads a per-sample metric value from the scorer's own detail where the scorer
  measured one, so a suite whose scorer measures several things at once reports each under its own
  key. A sample that did not measure a given figure is excluded from it with
  `not_measured_for_this_case` rather than contributing a zero (ADR-0016).
- Every capability name a benchmark test requires is validated against `ProviderCapabilities` when
  the registry is built — which is startup. An unknown name is treated as *unmet* at run time, so a
  manifest typo would otherwise have skipped its suite on every provider and reported a plausible
  reason for doing so.
- A `MockToolbox` offering `write_sandbox_file` must be given a `sandbox_root`; the default toolbox
  offers every tool except that one and cannot write at all. No shipped case writes, and no default
  directory is invented for one that might.
- `GET /api/v1/runs/{id}` gained `provenance` (served context and source, GPU attribution,
  telemetry overhead, prompt pack, fingerprint document) and `degradations`; metric objects gained
  `gpu_index`, `stddev` and `coefficient_of_variation`.
- Aggregation moved out of the run service into `freeweight.domain.aggregation`, which derives
  each metric from the stored samples through `freeweight.domain.metrics` and refuses to combine
  tests of different measurement classes into one run-level figure.
- A benchmark test that cannot enumerate its cases now fails only that test at *every* stage,
  including run creation and preparation, rather than failing the run (spec §13).

- Phase 5: the run engine, end to end against `FakeProvider`. `freeweight run start --suite
  native.echo` queues a run, executes it (claim → prepare → warm → execute → aggregate →
  complete), stores every raw sample, and streams progress to the browser and the terminal;
  `freeweight run list|show|cancel|wait` and the run list, detail and sample pages read the same
  data through the same service functions as `POST /api/v1/runs`, `GET /api/v1/runs`,
  `GET /api/v1/runs/{id}`, `POST /api/v1/runs/{id}/cancel`, `GET /api/v1/runs/{id}/tests`,
  `GET /api/v1/runs/{id}/tests/{test_id}/samples` and `GET /api/v1/runs/{id}/events`.
- Eight new tables and migration `0002`: `benchmark_suites`, `benchmark_tests`, `runs`,
  `run_tests`, `samples`, `metric_values`, `run_events` and `artifacts`. Results cascade downward
  and never delete identity upward (`ON DELETE RESTRICT` on every model, machine, descriptor and
  suite reference), and `ck_samples_score_null_unless_completed` states in DDL that a sample which
  is not `completed` cannot carry a score at all.
- The run and test state machines as explicit, enumerable transition tables
  (`freeweight.domain.run_state`), with every legal transition, every illegal one and the
  immutability of every terminal state asserted over all 81 (and 36) ordered pairs.
- Persisted run events with gap-free per-run sequences from 1, and `GET /api/v1/runs/{id}/events`
  (SSE) with `Last-Event-ID` replay. Events are committed before they are published, so replay,
  a reload mid-run and a restart all resume with no gap and no duplicate; the run detail page
  renders the sequence it got to, and the browser reconnects from exactly there.
- `native.echo`, a trivial deterministic self-test suite that exercises the whole engine on any
  provider. It measures FreeWeight, not the model: it declares no capabilities and emits no
  capability evidence.
- A single-threaded run scheduler with startup recovery. One GPU workload at a time is a property
  of the claim (`UPDATE … WHERE status = 'queued'`, refused while any run is in flight), so a
  `freeweight run start` typed while a server is serving queues the run and exits `7` rather than
  running it concurrently. A run left mid-flight by a killed process is recovered as `interrupted`
  — never `failed` — keeps its completed tests, and resumes from the exact (case, repetition) it
  had reached.
- Cancellation honoured at every phase boundary: `queued`, `preparing` and `warming` cancel
  outright, `running` enters `cancelling` and stops at the executor's next check. `Ctrl-C` during
  `freeweight run start` or `run wait` cancels cleanly, preserves committed samples and exits `6`.
- `[execution]` configuration section (`warmup_repetitions`, `measured_repetitions`,
  `cooldown_seconds`, `test_timeout_seconds`, `run_timeout_seconds`, `randomize_case_order`,
  `seed`, `gpu_index`, the idle-detection group, `on_idle_timeout`, `store_responses`), resolved
  once per run and frozen into the run record.

- Phase 4: telemetry and machine profile. The current host is profiled through SweatMeter once at
  server startup and upserted by fingerprint (`last_seen_at` refreshed on every later startup),
  populating the machines page for the first time. A single telemetry sampler, owned by the web
  application for as long as it serves, backs the telemetry bar shown on every page (CPU, RAM, GPU
  utilization and temperature, VRAM, power — `—` with a reason for anything unsupported, e.g. no
  GPU), `GET /api/v1/system/telemetry/stream` (SSE, `telemetry.sampled` events, 15 s heartbeat) and
  `GET /api/v1/system/status`. Two new health components, `gpu_telemetry` and `machine`, join
  `database` and `provider` on `GET /api/v1/health`, `freeweight health` and `freeweight doctor`; a
  machine with no GPU degrades the application without making it unavailable.
- Phase 3: model discovery through ModelRack exclusively. `freeweight models refresh` (and the
  models page's "Refresh from provider" action) lists every model the configured provider serves,
  upserts its canonical identity, and stores an immutable descriptor snapshot only when its content
  actually changed — a second refresh with nothing changed leaves the descriptor history exactly as
  long as it was. A digest present yields `identity_confidence = digest`; absent yields `name_only`;
  a name later gaining a digest upgrades that row in place rather than duplicating it; a *changed*
  digest creates a new identity and leaves the old one's history untouched. `freeweight models
  list|show` and the models list/detail pages, with `show`/`GET /models/{model_ref}` falling back to
  a live provider resolution when a reference (a bare name, an old tag, an unambiguous prefix) is not
  yet stored, recording the alias it resolved through rather than hiding it. The provider is
  constructed exactly once, in the composition root
  (`freeweight.infrastructure.providers.factory`) — the only module in this application that reaches
  provider HTTP code, asserted by a boundary test scanning the rest of the source tree.
- The `provider` health component (version and model count on success; `unavailable`/`degraded` with
  a reason otherwise), reported by `GET /api/v1/health`, `freeweight health` and `freeweight doctor`.
  An unreachable provider degrades the application; per Graceful Degradation §3, an optional
  component never makes it `unavailable` on its own — only the required `database` component can.
  With the provider down, the models page and `models list` still work: they read the last
  discovery attempt's recorded outcome rather than probing the provider on every view, so they say
  the data may be stale instead of pretending it is current.
- `provider.kind = "fake"`, constructing `modelrack.testing.FakeProvider` — alongside the production
  `"ollama"` adapter — so the running application, not just its unit tests, can be exercised with no
  GPU, no Ollama and no network (testing standards §1).
- Phase 1: `freeweight serve` starts a web server answering `GET /api/v1/health` and
  `GET /api/v1/version`, and rendering the application shell; `freeweight health --json` and
  `freeweight version --json` return the same data through the same service layer. Typer CLI with
  `serve`, `health`, `version`, `doctor`, and `config show|validate|init|path`. Typed settings with
  full precedence (defaults → file → environment → CLI) and source tracking, with the documented
  unsafe-binding refusals (non-loopback without tokens or `allowed_hosts`; `0.0.0.0` without
  `allow_lan_exposure`). Structured JSON/text logging with request-ID correlation. The standard
  error envelope, `Host`-header validation, and a request body size limit.
- Phase 2: the storage foundation. SQLAlchemy models for `machines`, `models`, `model_descriptors`,
  `runtime_profiles`, `settings` and `api_tokens`, with the `models` table's partial unique index
  enforcing at most one `name_only` identity per `(provider_kind, provider_model_name)` and a
  repository method that upgrades that row in place when a digest later arrives, rather than
  duplicating it. Engine and session plumbing written as if it were the future `weightsdb` package:
  dialect-correct pragmas (SQLite `foreign_keys`/`WAL`/`busy_timeout`/`synchronous`, PostgreSQL
  `statement_timeout`/`lock_timeout`/`application_name`) applied on every connection including
  after a pool reconnect, `BEGIN IMMEDIATE` transactions on SQLite, a portable `UtcDateTime` and
  `PortableJSON`, and the one sanctioned dialect-correct upsert. Alembic migration `0001` creates
  the full Phase 2 schema; `MigrationRunner` wraps it with an autogenerate parity check, and
  `freeweight db upgrade` takes an automatic backup first and restores it on failure (SQLite).
  `freeweight db upgrade|status|backup|restore|vacuum`. The `database` health component, backed by
  the same startup revision check as `freeweight serve`.
- Phase 2: the machines and models pages, server-rendered from the real tables, with the empty and
  error states UI standards §6 requires. Both are legitimately empty until Phase 3 (models) and
  Phase 4 (machines) fill them.
- `storage.backup_retention` (default 5) and `storage.statement_timeout_ms` (PostgreSQL only,
  unset by default).
- `freeweight db status` reports database size, and `freeweight db vacuum` previews the space it
  expects to reclaim before running and reports what it actually reclaimed (database standards §7,
  §8).
- Automatic pre-migration backups carry the schema revision in their name and rotate to
  `storage.backup_retention` generations, with each rotation logged (database standards §7).
- Repository scaffold generated from the suite's development plan.
- A read-only transaction mode (``session_scope(..., read_only=True)``, ``Database.read()``). On
  SQLite it takes a deferred ``BEGIN`` instead of ``BEGIN IMMEDIATE``, and is enforced with
  ``PRAGMA query_only`` rather than trusted.
- ``Database``, the application's connection handle. The web application creates one in its
  lifespan and disposes it at shutdown; a CLI command owns one for the length of the command.
  Service functions take a handle and no longer build engines of their own.
- ``storage.statement_timeout_ms`` (PostgreSQL only; also applied as ``lock_timeout``).

### Fixed
- `tests/e2e/test_run_journey.py` raced the event publisher by exactly one event: a run reaches its
  terminal status in the database *before* its terminal event is published — deliberately, so a
  client can never see a closed stream without a terminal frame — and the test compared event
  counts across that window. The test now waits for the stream to go quiet; the ordering it was
  testing is unchanged.
- `freeweight db restore` silently restored nothing when the database had uncheckpointed writes in
  its WAL sidecar. The main database file was replaced, the stale `<db>-wal` was not, and the next
  reader replayed it straight back on top — so the writes the restore was called to undo survived,
  while the command reported success. Restore now checkpoints, releases the pool and removes the
  sidecars before the swap. This also affected the automatic restore after a failed migration, and
  therefore database standards §7's byte-identical guarantee.
- `freeweight db restore` deleted its `.pre-restore` rollback copy without ever checking that the
  restored file opened, and never verified that the backup sat at a revision this build knows.
  Both are now done, and a restored file that fails its integrity check puts the original back.
- `ModelRepository.upsert_identity` raised a raw `IntegrityError` out of the repository when a
  provider reported a digest, then omitted it, then reported it again — a sequence Ollama produces
  in practice, and the first thing Phase 3's discovery loop would have hit.
- Every PostgreSQL connection carrying an `application_name` failed outright: `SET x = %s` is a
  syntax error, since PostgreSQL's `SET` takes no bind parameter. It now uses `set_config`. The
  setting was also never passed by `build_engine`, so it had no effect even where it worked.
- `freeweight db backup` with no `--output` failed on PostgreSQL with "Expected a SQLite engine",
  and `freeweight db restore` failed the same way instead of naming the `pg_restore` command.
- `storage.auto_migrate` defaulted to true on both dialects. It now defaults to false on
  PostgreSQL, where a failed migration cannot be rolled back automatically (database standards
  §5.1, §7); an explicit setting is still honoured on either dialect.
- `freeweight db backup` no longer writes an empty, successful-looking backup when the SQLite
  database does not exist, and creates the backup file with mode `0600` before writing data into
  it rather than narrowing the permissions afterwards.
- The machines and models pages return their error state rather than a 500 when the database
  cannot be read.
- Every transaction took SQLite's single write lock, including transactions that only read, so two
  concurrent page views queued behind each other for up to ``busy_timeout`` and then failed. WAL's
  concurrent reads are back for the paths that only read. ``BEGIN IMMEDIATE`` stays for writes: a
  deferred read-then-write transaction gets ``SQLITE_BUSY_SNAPSHOT``, which ``busy_timeout`` does
  not apply to — measured, it fails in 0.0 ms with a 2000 ms timeout configured — and whose only
  escape is discarding work and starting over.
- The application rebuilt an engine, and therefore a connection pool and a compiled-statement
  cache, for every database read — including once per page view. Measured on this schema, the same
  query cost 0.95 ms that way against 0.12 ms through a live handle on SQLite, and 7.41 ms against
  0.38 ms on PostgreSQL, where each call also opened a fresh backend connection and made the
  configured ``pool_size`` meaningless.
- ``GET /api/v1/health`` reported on a connection it opened for the check rather than the one the
  server serves requests from.

### Changed
- The migration suite and the repository suite both run on SQLite **and** PostgreSQL in CI, rather
  than one PostgreSQL smoke test alongside a SQLite-only suite. `upsert()`'s `ON CONFLICT` branch
  and the savepoint-retry path that exists for PostgreSQL's MVCC were previously executed by
  nothing.
- `MigrationOutcome.restored`, which could never be `True`, is replaced by
  `restore_on_failure_available`, which states the dialect difference database standards §7
  requires to be stated rather than papered over. `MigrationOutcome` also carries `pruned_backups`.
- `GET /api/v1/system/status` now reports the real `active_run` and `queue_depth`. Both are `null`
  when the queue cannot be read — a database behind head, say — rather than a reassuring `0`.
- Widened the `sweatmeter` pin to `>=0.4,<0.5`. SweatMeter's first published release is `0.4.0`
  (`0.3.0` completed its development plan but never reached the index), and it adds the in-process
  NVML GPU backend, selected automatically wherever the optional `pynvml` extra is installed.

