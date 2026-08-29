# Phase 11 — issues to address

Written at the end of Phase 11 (capability evidence and the LoadCoach contract; M3, `1.0.0rc1`).
Each entry is something a later phase, a docs change, an upstream package or a release step has to
resolve. Nothing here blocks Phase 11's acceptance criteria; everything here would become a defect
if it were forgotten.

The foot of the file records what Phase 8's open items now look like: §7, §8 and §9 are closed here.

---

## Status — 2026-08-28

| # | Issue | Status |
|---|---|---|
| 1 | `setspec>=0.3,<0.4` is pinned but 0.3.0 is not on PyPI yet | **Owner: you.** CI's `install-check` and `build` cannot resolve until it is. |
| 2 | FreeWeight has no requirements lockfiles and `release.yml` has no `environment` or TestPyPI rehearsal | **Mostly closed.** `release.yml` gained `environment: pypi` and the manual `publish-testpypi` job; `requirements/release.{in,lock}` are committed and verified. `ci.lock` is still owed and is blocked on `setspec 0.3.0` reaching PyPI. |
| 3 | `native.agent` declares capability `agent_behaviour`, which is not a vocabulary term | **Needs a decision.** |
| 4 | Manifest `capabilities` lists and `capability_weights.toml` are two authorities | **Needs a decision.** |
| 5 | Every normalisation scale in `capability_weights.toml` is a judgement call | **Open — by design.** Revisit with real data. |
| 6 | `consistency_factor` uses one dispersion for continuous and pass/fail metrics | **Needs a decision.** |
| 7 | A bundle's `source_id` is the newest machine's fingerprint, not a persisted instance identity | **Open.** |
| 8 | Two goals contributing to one shipped capability share one identity group | **Open — limitation.** |
| 9 | `GET /api/v1/evidence` pages in memory | **Open — bounded, documented.** |
| 10 | Spec §12 lists `[external]` and `[sandbox]` sections the settings model does not have | **Needs a docs or code change.** Pre-existing. |
| 11 | The OpenAPI snapshot's path, and "changed without a changelog entry" | **Needs a docs change.** |
| 12 | UI/UX §12's information architecture has no Evidence row | **Needs a docs change.** |
| 13 | The M2 tag was never cut; the version goes `0.1.0 → 1.0.0rc1` | **Owner: you.** |
| 14 | Tagging 1.0.0rc1 | **Owner: you.** Commands below. |

---

## Dependencies and release

### 1. `setspec>=0.3,<0.4` is pinned before 0.3.0 exists on PyPI

The prompt was explicit that the pin moves in this release, and the `0.2` pin's own comment
promised it. It is done — and it means the `build` and `install-check` jobs, and any `pip install
freeweight` from a clean index, cannot resolve until `setspec 0.3.0` is published from the tag
recorded in `py/SetSpec/PHASE4_ISSUES.md` §6. Locally the editable install of the workspace's
SetSpec (at `0.3.0`) is what the tests ran against. Order of operations: publish SetSpec, then push
FreeWeight.

### 2. `ci.lock` is owed, and is blocked on `setspec 0.3.0`

**Done.** `release.yml` now carries `environment: pypi` on the release job — the OIDC exchange has
nothing to match without it and the publish is rejected — and the manual `publish-testpypi` job
packaging standards §6 requires before a package's **first** real release, which `freeweight`
(an unclaimed distribution name) is about to have. `requirements/release.in` and `release.lock` are
committed; `release.yml`'s two jobs and CI's `build` job install from the lock and build with
`--no-isolation`, so the wheel that ships is built by the backend CI checked. Verified end to end:
`--require-hashes` install into a clean 3.13 interpreter → `python -m build --no-isolation` →
`twine check` passes on both artifacts, and `pip-audit` reports the locked set clean. The lock's
body — pins *and* hashes — is byte-identical to SetSpec's, which was resolved against PyPI.

**Still owed: `requirements/ci.lock`.** `pyproject.toml` pins `setspec>=0.3,<0.4`, which is not on
PyPI, so `pip-compile` resolves it from the workspace checkout and writes hashes for a **locally
built** artifact. Those cannot match the wheel CI publishes from the tag, so every
`--require-hashes` install would fail with a mismatch — a lock that looks authoritative and is
wrong, which is worse than none. It lands in the commit after `setspec 0.3.0` is published; until
then CI's non-build jobs install from the ranges, as they did before, and `security` audits
`release.lock` only.

One thing that generation surfaced and is now recorded in `requirements/README.md`: this project's
`dev` extra depends on `freeweight[postgresql]`, and `pip-compile` resolves that self-reference to
`freeweight @ file:///…` — a local path that cannot be hashed and that would name one developer's
checkout in a committed file. `--unsafe-package freeweight` excludes the project from its own lock;
`psycopg` still appears, because it is reached through the extra either way. The command in the
README carries the flag.

### 13. `0.1.0 → 1.0.0rc1`

`__about__.py` said `0.1.0` through ten delivered phases; roadmap §6 puts M2 at `0.9-beta`
(`0.9.0b0`) and M3 at `1.0-rc`. The M2 tag was never cut, so `0.9.0b0` is skipped rather than
back-dated — the changelog says so. If you would rather have the M2 tag exist for the record, tag
the commit before this phase's as `v0.9.0b0` first; nothing depends on it.

### 14. Tagging 1.0.0rc1

Nothing was committed, tagged or published. From the FreeWeight repository, after SetSpec 0.3.0 is
on PyPI and CI is green on `main`:

```bash
cd ~/ai/suite/FreeWeight
git add -A
git commit -m "feat(freeweight): capability evidence and the LoadCoach contract (Phase 11, 1.0.0rc1)"
git push origin main
git tag -a v1.0.0rc1 -m "freeweight 1.0.0rc1 — M3: capability evidence, contract freeze"
git push origin v1.0.0rc1
```

The docs repository also changed (canonical copies of spec §12, api.md §6, data-model.md,
subjective-goals.md §3.3); commit those from `~/ai/suite/docs` (or `AiSuite/`, whichever is the
git root) with a message naming Phase 11.

---

## Needs a decision

### 3. `native.agent` declares `agent_behaviour`

`benchmarks/agent/manifest.json` lists `capabilities: ["agent_behaviour"]` (and the same string as
its category). `agent_behaviour` is not a root in SetSpec's vocabulary; `agentic` is. Nothing
validates a manifest's `capabilities` against the vocabulary — `_check_declared_capabilities` in
`services/runs.py` checks *provider* capabilities, a different thing — so the mismatch has been
invisible. `capability_weights.toml` routes the agent suite's metrics to `reasoning` and `agentic`,
so the evidence is right; the manifest is what is wrong. Fix the manifest (`["agentic",
"reasoning"]`), and see §4 for whether the field should exist at all.

### 4. Two authorities for "which capability does this suite feed"

Every shipped manifest carries a `capabilities` list, and `BenchmarkManifest.capabilities` says it
is "the capability IDs this suite contributes evidence to". Phase 11 made `capability_weights.toml`
the thing that actually decides that, with weights and scales the manifest cannot express — and the
evidence service never reads the manifest's list. Two authorities drift; §3 is the first instance.
Options: (a) make the mapping file the only authority and drop `capabilities` from the manifests
and the catalog's field list; (b) keep the manifest list as a *declaration* and add a contract test
that every suite the mapping names declares every capability it feeds, and vice versa. (b) keeps
the suite author's intent visible beside the suite; (a) is one fewer thing. Either way the catalog
(§5's field list, §6's table) needs the sentence that says which.

### 6. One dispersion for two kinds of metric

ADR-0017 defines `consistency_factor` from the coefficient of variation for a continuous metric and
from the *disagreement rate* for a pass/fail one. FreeWeight's `metric_values` rows carry one
`coefficient_of_variation`, computed over the samples whatever their kind, and the evidence
service uses it for both. For a mean of 0/1 samples the CV is a monotone function of the
disagreement rate but not the same number: at 90 % agreement the CV is 0.33 and the disagreement
rate 0.10, so a pass/fail metric is penalised harder than the ADR intends. Two fixes: store a
`disagreement_rate` beside the CV at aggregation (a `metric_values` column, Phase 12 with
WeightsDB), or declare per metric in the manifest which statistic applies. Until one lands, the
factor errs toward *less* confidence, which is the safe direction.

### 8. Two goals into one shipped capability

When two calibrated goals both declare `contributes_to: "creative_writing"`, the blended record
carries one goal identity group (`goal_hash`, `judge_set`, `calibration`) — the goal with the
higher validity factor — because `capability.evidence` has room for one. Both goals still appear
in `contributing_metrics` and both factors blend into `judge_validity_factor`, so nothing is lost
from the number; what is lost is the second goal's hash as a separation input. A consumer that
separates on `goal_hash` will treat the blend as belonging to the first goal only. Rare enough to
record rather than redesign; the honest fix is a `contributing_goals` list on the contract, which
is a `capability.evidence` minor.

---

## Needs a documentation change

### 10. `[external]` and `[sandbox]` exist in spec §12 and nowhere else

The generated reference cannot document sections the settings model does not have, and it does not
have `[external]` or `[sandbox]` — though `CONFIG_ONLY_KEYS` in `services/settings.py` names
`external.root` and `sandbox.tier`. Phase 9's sandbox tiers and Phase 13's external adapters own
them. Until then spec §12 describes configuration that `config.toml` rejects as unknown. Either add
the two sections to the model (with the fields §12 lists, defaulted) or mark them in §12 as
scheduled.

### 11. Where the OpenAPI snapshot lives, and what "changes without a changelog entry" means

API standards §11 says `docs/api/openapi-v1.json`; testing standards §8.4 says `<app>/api/openapi-v1.json`
as package data with an `api_snapshot()` accessor; the pre-existing I3 milestone test looked for
`docs/openapi.json`. Phase 11 followed the test that already existed, so `docs/openapi.json` is the
committed snapshot and the `docs` CI job fails on drift. The standard's stronger promise — the
build fails when the document *changes without a changelog entry* — is not mechanised; the diff
is what a reviewer sees. Pick one path in the two standards, and say whether the package-data copy
(which LoadCoach's own contract tests would consume) is owed at M4.

### 12. The primary navigation has an Evidence entry

UI/UX standards §12's information-architecture table lists FreeWeight's navigation as Dashboard,
Run, Results, Models, Database, Settings. The application already carried Machines, Compare and
Goals beyond that list; Phase 11 adds Evidence, because the LoadCoach integration point needs a
page a person can read. Amend the table.

---

## Open, by design

### 5. Every scale in `capability_weights.toml` is a judgement call

`speed` reaches full marks at 100 decode tokens/s; `latency` reaches zero at two seconds to first
token; `memory_efficiency` reaches zero at 256 KiB of KV cache per token; `energy_efficiency` earns
full marks at one token per joule. None has an empirical basis, exactly as ADR-0017 says of its own
parameters, which is why they are configuration with a version and why a customised file derives a
different `policy_version`. ADR-0017's *revisit when* applies: fit them once real routing data
exists. Until then the shipped defaults are documented as provisional in the file's header.

### 7. `source_id`

A bundle's `source_id` is `freeweight:<fingerprint>` of the most recently seen machine — the one
this process runs on — chosen because it is the one stable identity the database holds without
writing anything on a read. It is not a persisted instance identity: two FreeWeight installations
on one machine would share it, and a machine change changes it. ADR-0022's own revisit trigger — a
second evidence producer, a federated import — is when this needs to become a real identifier
stored at first start. Not before.

### 9. In-memory pagination

`GET /api/v1/evidence` reads every matching row and cuts the page in memory, with a cursor over the
total order `(capability_id, id)`. The collection is bounded — one row per model × profile ×
machine × capability × policy — and a consumer pulls it through the bundle anyway. If a machine
ever holds thousands of subjects, switch to a keyset query; the cursor's shape is already the
keyset.

---

## Phase 8's open items, revisited

| # | Issue | Status |
|---|---|---|
| 7 | `capability_evidence` does not exist, so the gate's "no row" is asserted conditionally | **Closed.** Migration `0007`; the assertion is direct, and end to end for both halves of a `contributes_to` goal (`tests/contract/test_evidence_export.py::TestTheGateWithholdsBothHalves`). |
| 8 | `contributes_to` is stored and linted but never emitted twice | **Closed.** Emitted as `user.<slug>` and as one weighted source inside the shipped capability, never only as the latter (`TestContributesToEmitsTwice`). |
| 9 | Rung-4 (`human`) criteria are parsed and skipped | **Closed.** `/runs/{id}/grade` and `freeweight goals grade --run` (`tests/integration/test_human_grading.py`). |
| 19 | Deleting a goal removes its pack directory without a backup | **Open.** Still needs a decision. |
