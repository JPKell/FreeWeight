"""``native.judge``'s bias measurement, against synthetic judges whose bias is known.

The phase's own test list names three cases, and each has a class here:

* **position bias detected on a synthetic order-biased judge** — a judge that always picks whatever
  is presented first must show ``swap_consistency`` 0 and ``position_preference_rate`` 1;
* **transitivity violations counted** — a judge that says A>B, B>C and C>A must record exactly one
  violation, and a judge that merely ties must record none;
* **self-preference measured with and without anonymization** — the delta is the difference between
  the two conditions, not the preference in either.

The fourth entry on that list — *judged scores carry the judge's identity, prompt version and bias
metrics* — is :class:`TestAJudgedScoreCarriesItsInstrument`, which asserts the linkage every judged
number will carry from Phase 8B onward.

The judges here are two-line functions, which is the point: a position-biased juror is a test case
rather than a field report.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.judging import (
    BIAS_METRIC_KEYS,
    JUDGE_SUITE_KEY,
    REASON_NOT_INSTALLED,
    REASON_REMOTE_NOT_PERMITTED,
    REASON_SELF_JUDGING,
    JudgeChoice,
    JudgeRecord,
    JudgeTrial,
    agreement_rate,
    blind_labels,
    eligible_jurors,
    judge_benchmark_reference,
    majority_choice,
    parse_choice,
    present,
    presentation_orders,
    randomized_order,
)
from freeweight.domain.scorers.judge import JudgeExpectation, JudgeScorer, judge_metrics

_LABELS = ("A", "B")


def _trial(
    ordinal: int,
    subjects: tuple[str, str],
    choice: JudgeChoice,
    group: str = "",
) -> JudgeTrial:
    return JudgeTrial(ordinal=ordinal, order=_LABELS, subjects=subjects, choice=choice, group=group)


def _always_first(subjects: tuple[str, str]) -> JudgeChoice:
    """A synthetic judge that always prefers whatever was presented first."""
    del subjects
    return JudgeChoice.FIRST


def _always_subject(wanted: str) -> Callable[[tuple[str, str]], JudgeChoice]:
    """A synthetic judge that always prefers one particular subject, wherever it appears."""

    def choose(subjects: tuple[str, str]) -> JudgeChoice:
        return JudgeChoice.FIRST if subjects[0] == wanted else JudgeChoice.SECOND

    return choose


def _record(
    judge: Callable[[tuple[str, str]], JudgeChoice],
    plan: list[tuple[tuple[str, str], str]],
) -> JudgeRecord:
    return JudgeRecord(
        trials=tuple(
            _trial(ordinal, subjects, judge(subjects), group)
            for ordinal, (subjects, group) in enumerate(plan)
        )
    )


class TestPositionBiasIsDetected:
    """A judge that always picks the first-presented answer."""

    def test_the_swap_catches_it(self) -> None:
        expectation = JudgeExpectation.from_json({"kind": "position"})
        record = _record(_always_first, [(("better", "worse"), ""), (("worse", "better"), "")])
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["swap_consistency"] == 0.0
        assert metrics["position_preference_rate"] == 1.0
        assert score == 0.0

    def test_a_consistent_judge_expresses_no_positional_preference_to_measure(self) -> None:
        # The rate's denominator is the inconsistent pairs. A consistent pair contributes nothing
        # to it and is excluded rather than counted as zero preference (ADR-0016).
        expectation = JudgeExpectation.from_json({"kind": "position"})
        record = _record(
            _always_subject("better"), [(("better", "worse"), ""), (("worse", "better"), "")]
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["swap_consistency"] == 1.0
        assert "position_preference_rate" not in metrics
        assert score == 1.0

    def test_a_second_position_bias_is_the_same_defect_pointing_the_other_way(self) -> None:
        # Chose the second-presented answer both times: equally inconsistent, and the direction
        # figure says which way rather than how much.
        expectation = JudgeExpectation.from_json({"kind": "position"})
        record = JudgeRecord(
            trials=(
                _trial(0, ("better", "worse"), JudgeChoice.SECOND),
                _trial(1, ("worse", "better"), JudgeChoice.SECOND),
            )
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["swap_consistency"] == 0.0
        assert metrics["position_preference_rate"] == 0.0
        assert score == 0.0

    def test_a_judge_that_ties_both_ways_is_consistent(self) -> None:
        # The swap did not move it, which is what consistency means. Scoring a double tie as
        # inconsistent would report indecision as bias.
        expectation = JudgeExpectation.from_json({"kind": "position"})
        record = JudgeRecord(
            trials=(
                _trial(0, ("better", "worse"), JudgeChoice.TIE),
                _trial(1, ("worse", "better"), JudgeChoice.TIE),
            )
        )
        _score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["swap_consistency"] == 1.0
        assert "position_preference_rate" not in metrics

    def test_a_position_biased_judge_still_looks_accurate_on_one_presentation(self) -> None:
        # Which is exactly why the suite asks twice: pairwise accuracy alone cannot see this.
        expectation = JudgeExpectation.from_json({"kind": "pairwise", "gold": "better"})
        one_way = _record(_always_first, [(("better", "worse"), "")])
        _score, metrics, _evidence = judge_metrics(expectation, one_way)
        assert metrics["pairwise_accuracy"] == 1.0
        both_ways = _record(_always_first, [(("better", "worse"), ""), (("worse", "better"), "")])
        _score, both, _evidence = judge_metrics(expectation, both_ways)
        assert both["pairwise_accuracy"] == 0.5


class TestTransitivityViolationsAreCounted:
    """A>B, B>C, and then C>A."""

    _ORDERING = {"kind": "transitivity", "ordering": ["best", "middle", "worst"]}

    def test_a_cycle_is_one_violation(self) -> None:
        expectation = JudgeExpectation.from_json(self._ORDERING)
        record = JudgeRecord(
            trials=(
                _trial(0, ("best", "middle"), JudgeChoice.FIRST, "ab"),
                _trial(1, ("middle", "worst"), JudgeChoice.FIRST, "bc"),
                _trial(2, ("best", "worst"), JudgeChoice.SECOND, "ac"),
            )
        )
        score, metrics, evidence = judge_metrics(expectation, record)
        assert metrics["transitivity_violation_rate"] == 1.0
        assert score == 0.0
        assert evidence["chain"] == {"ab": "best", "ac": "worst", "bc": "middle"}

    def test_a_coherent_chain_is_no_violation(self) -> None:
        expectation = JudgeExpectation.from_json(self._ORDERING)
        record = JudgeRecord(
            trials=(
                _trial(0, ("best", "middle"), JudgeChoice.FIRST, "ab"),
                _trial(1, ("middle", "worst"), JudgeChoice.FIRST, "bc"),
                _trial(2, ("best", "worst"), JudgeChoice.FIRST, "ac"),
            )
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["transitivity_violation_rate"] == 0.0
        assert score == 1.0

    def test_the_judges_own_reversed_chain_is_still_checked(self) -> None:
        # It ranked the corpus backwards, which is a correctness failure and not a coherence one
        # -- but claiming best>worst after middle>best and worst>middle is incoherent.
        expectation = JudgeExpectation.from_json(self._ORDERING)
        record = JudgeRecord(
            trials=(
                _trial(0, ("best", "middle"), JudgeChoice.SECOND, "ab"),
                _trial(1, ("middle", "worst"), JudgeChoice.SECOND, "bc"),
                _trial(2, ("best", "worst"), JudgeChoice.FIRST, "ac"),
            )
        )
        _score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["transitivity_violation_rate"] == 1.0

    def test_a_tie_in_the_chain_is_indecision_and_not_a_violation(self) -> None:
        expectation = JudgeExpectation.from_json(self._ORDERING)
        record = JudgeRecord(
            trials=(
                _trial(0, ("best", "middle"), JudgeChoice.FIRST, "ab"),
                _trial(1, ("middle", "worst"), JudgeChoice.TIE, "bc"),
                _trial(2, ("best", "worst"), JudgeChoice.FIRST, "ac"),
            )
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert score is None
        assert "transitivity_violation_rate" not in metrics


class TestSelfPreferenceWithAndWithoutAnonymization:
    """The delta between two otherwise-identical conditions."""

    _EXPECTATION = {"kind": "self_preference", "own": "own"}

    def test_preferring_its_own_answer_only_once_told_is_the_bias(self) -> None:
        expectation = JudgeExpectation.from_json(self._EXPECTATION)
        record = JudgeRecord(
            trials=(
                _trial(0, ("own", "reference"), JudgeChoice.SECOND, "anonymized"),
                _trial(1, ("reference", "own"), JudgeChoice.FIRST, "anonymized"),
                _trial(2, ("own", "reference"), JudgeChoice.FIRST, "attributed"),
                _trial(3, ("reference", "own"), JudgeChoice.SECOND, "attributed"),
            )
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["self_preference_anonymized"] == 0.0
        assert metrics["self_preference_attributed"] == 1.0
        assert metrics["self_preference_delta"] == 1.0
        assert score == 0.0

    def test_preferring_its_own_answer_in_both_conditions_is_not_a_bias(self) -> None:
        # It may simply be right. What is measured is the *change* attribution makes.
        expectation = JudgeExpectation.from_json(self._EXPECTATION)
        record = _record(
            _always_subject("own"),
            [
                (("own", "reference"), "anonymized"),
                (("reference", "own"), "anonymized"),
                (("own", "reference"), "attributed"),
                (("reference", "own"), "attributed"),
            ],
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["self_preference_delta"] == 0.0
        assert score == 1.0

    def test_a_judge_that_becomes_harsher_on_its_own_work_is_not_penalized(self) -> None:
        expectation = JudgeExpectation.from_json(self._EXPECTATION)
        record = JudgeRecord(
            trials=(
                _trial(0, ("own", "reference"), JudgeChoice.FIRST, "anonymized"),
                _trial(1, ("own", "reference"), JudgeChoice.SECOND, "attributed"),
            )
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["self_preference_delta"] == -1.0
        assert score == 1.0

    def test_one_missing_condition_leaves_the_delta_unmeasured(self) -> None:
        expectation = JudgeExpectation.from_json(self._EXPECTATION)
        record = JudgeRecord(
            trials=(_trial(0, ("own", "reference"), JudgeChoice.FIRST, "attributed"),)
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert score is None
        assert "self_preference_delta" not in metrics


class TestVerbosityRepetitionAndStyle:
    """The three remaining rows of catalog §3.11."""

    def test_a_judge_that_always_picks_the_longer_answer(self) -> None:
        expectation = JudgeExpectation.from_json({"kind": "verbosity", "disfavoured": "verbose"})
        record = _record(
            _always_subject("verbose"),
            [(("concise", "verbose"), ""), (("verbose", "concise"), "")],
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["verbosity_preference_rate"] == 1.0
        assert score == 0.0

    def test_a_judge_that_ties_on_identical_content_shows_no_style_preference(self) -> None:
        expectation = JudgeExpectation.from_json({"kind": "style", "disfavoured": "flourish"})
        record = JudgeRecord(
            trials=(
                _trial(0, ("plain", "flourish"), JudgeChoice.TIE),
                _trial(1, ("flourish", "plain"), JudgeChoice.TIE),
            )
        )
        score, metrics, _evidence = judge_metrics(expectation, record)
        assert metrics["style_preference_rate"] == 0.0
        assert score == 1.0

    def test_repetition_agreement_is_the_modal_share(self) -> None:
        expectation = JudgeExpectation.from_json({"kind": "repetition"})
        record = JudgeRecord(
            trials=(
                _trial(0, ("better", "worse"), JudgeChoice.FIRST),
                _trial(1, ("better", "worse"), JudgeChoice.FIRST),
                _trial(2, ("better", "worse"), JudgeChoice.SECOND),
            )
        )
        score, metrics, evidence = judge_metrics(expectation, record)
        assert metrics["repetition_agreement_rate"] == pytest.approx(2 / 3)
        assert score == pytest.approx(2 / 3)
        assert evidence["majority_verdict"] == "first"


class TestUnparseableVerdicts:
    """A judge that did not answer has not disagreed; it has not answered."""

    def test_they_are_excluded_from_agreement(self) -> None:
        assert (
            agreement_rate([JudgeChoice.FIRST, JudgeChoice.FIRST, JudgeChoice.UNPARSEABLE]) == 1.0
        )

    def test_all_unparseable_is_no_measurement_rather_than_zero(self) -> None:
        assert agreement_rate([JudgeChoice.UNPARSEABLE]) is None
        assert majority_choice([JudgeChoice.UNPARSEABLE]) is JudgeChoice.UNPARSEABLE

    def test_an_even_split_reduces_to_a_tie_rather_than_iteration_order(self) -> None:
        assert majority_choice([JudgeChoice.FIRST, JudgeChoice.SECOND]) is JudgeChoice.TIE

    def test_a_case_with_no_usable_verdict_is_unscoreable(self) -> None:
        expectation = JudgeExpectation.from_json({"kind": "position"})
        record = JudgeRecord(
            trials=(
                _trial(0, ("better", "worse"), JudgeChoice.UNPARSEABLE),
                _trial(1, ("worse", "better"), JudgeChoice.UNPARSEABLE),
            )
        )
        score, _metrics, evidence = judge_metrics(expectation, record)
        assert score is None
        assert evidence["usable_trials"] == 0


class TestParsingAVerdict:
    """Two accepted forms, and prose is not one of them."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"choice": "A", "reason": "clearer"}', JudgeChoice.FIRST),
            ('{"verdict": "b"}', JudgeChoice.SECOND),
            ("VERDICT: B", JudgeChoice.SECOND),
            ("verdict = tie", JudgeChoice.TIE),
            ('{"choice": "neither"}', JudgeChoice.TIE),
            ("I think A is better, though B has a nicer tone.", JudgeChoice.UNPARSEABLE),
            ("", JudgeChoice.UNPARSEABLE),
            ('{"choice": "C"}', JudgeChoice.UNPARSEABLE),
            ("{not json at all", JudgeChoice.UNPARSEABLE),
        ],
    )
    def test_forms(self, text: str, expected: JudgeChoice) -> None:
        assert parse_choice(text, labels=_LABELS) is expected

    def test_a_presentation_with_no_labels_is_a_defect_not_a_verdict(self) -> None:
        with pytest.raises(ValueError, match="labels"):
            parse_choice("VERDICT: A", labels=())


class TestBlindingAndOrder:
    """What the judge is shown, and in what order."""

    def test_labels_replace_identities(self) -> None:
        assert blind_labels(("qwen", "gemma")) == ("A", "B")

    def test_more_subjects_than_letters_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            blind_labels(tuple(str(index) for index in range(9)))

    def test_a_pair_is_presented_both_ways_and_only_both_ways(self) -> None:
        assert presentation_orders(("a", "b")) == ((0, 1), (1, 0))

    def test_a_three_way_comparison_is_refused(self) -> None:
        with pytest.raises(ValueError, match="three-way"):
            presentation_orders(("a", "b", "c"))

    def test_the_swap_differs_from_the_original_in_order_alone(self) -> None:
        first = present(("a", "b"), ("alpha", "beta"), (0, 1))
        second = present(("a", "b"), ("alpha", "beta"), (1, 0))
        assert first.rendered() == "ANSWER A\nalpha\n\nANSWER B\nbeta"
        assert second.rendered() == "ANSWER A\nbeta\n\nANSWER B\nalpha"
        assert second.subjects == ("b", "a")

    def test_a_presentation_missing_a_text_is_refused(self) -> None:
        with pytest.raises(ValueError, match="one text per subject"):
            present(("a", "b"), ("alpha",), (0, 1))

    def test_order_randomization_is_reproducible(self) -> None:
        items = tuple(range(12))
        assert randomized_order(items, seed_material="case-1") == randomized_order(
            items, seed_material="case-1"
        )
        assert randomized_order(items, seed_material="case-1") != randomized_order(
            items, seed_material="case-2"
        )


class TestJurorSelection:
    """Who may judge, and why not.

    The two refusals ADR-0031 §4 names, decided in the domain so the jury service, the CLI and the
    API cannot disagree about them.
    """

    def test_a_model_never_judges_its_own_output(self) -> None:
        verdicts = eligible_jurors(["a", "b"], candidate="a")
        by_id = {verdict.model_canonical_id: verdict for verdict in verdicts}
        assert by_id["a"].eligible is False
        assert REASON_SELF_JUDGING in by_id["a"].reasons
        assert by_id["b"].eligible is True

    def test_a_remote_juror_needs_the_opt_in(self) -> None:
        refused = eligible_jurors(["r"], candidate=None, remote={"r": True})
        assert refused[0].reasons == (REASON_REMOTE_NOT_PERMITTED,)
        allowed = eligible_jurors(["r"], candidate=None, remote={"r": True}, allow_remote=True)
        assert allowed[0].eligible is True

    def test_a_requested_juror_that_is_not_installed_is_named(self) -> None:
        verdicts = eligible_jurors(["a"], candidate=None, requested=["a", "ghost"])
        assert verdicts[1].model_canonical_id == "ghost"
        assert verdicts[1].reasons == (REASON_NOT_INSTALLED,)

    def test_every_reason_is_recorded_not_just_the_first(self) -> None:
        verdicts = eligible_jurors(
            [], candidate="ghost", requested=["ghost"], remote={"ghost": True}
        )
        assert verdicts[0].reasons == (
            REASON_NOT_INSTALLED,
            REASON_REMOTE_NOT_PERMITTED,
            REASON_SELF_JUDGING,
        )


class TestAJudgedScoreCarriesItsInstrument:
    """Judged scores carry the judge's identity, prompt version and bias metrics."""

    def test_the_link_names_the_jury_the_prompt_and_the_judge_benchmark(self) -> None:
        link = judge_benchmark_reference(
            ["ollama/qwen3:14b@sha256:" + "ab" * 32],
            prompt_id="goals.judge.rubric",
            prompt_version="1.0.0",
            prompt_sha256="sha256:" + "cd" * 32,
            remote=False,
        )
        assert link["jurors"] == ["ollama/qwen3:14b@sha256:" + "ab" * 32]
        assert link["prompt_id"] == "goals.judge.rubric"
        assert link["prompt_version"] == "1.0.0"
        assert link["remote"] is False
        assert link["judge_benchmark"]["suite_key"] == JUDGE_SUITE_KEY
        assert "self_preference_delta" in link["judge_benchmark"]["metric_keys"]

    def test_the_named_bias_metrics_are_the_ones_the_suite_produces(self) -> None:
        from freeweight.benchmarks.judge.benchmark import load_suite_manifest

        declared = {
            str(entry["metric_key"]) for entry in load_suite_manifest().body.get("metrics", ())
        }
        assert set(BIAS_METRIC_KEYS) <= declared


class TestTheScorerEndToEnd:
    """What the run engine actually hands the scorer."""

    def _case(self, expectation: object) -> BenchmarkCase:
        return BenchmarkCase(case_id="c", ordinal=0, prompt="p", expectation={"judge": expectation})

    def test_a_serialized_record_round_trips(self) -> None:
        record = JudgeRecord(
            trials=(
                _trial(0, ("better", "worse"), JudgeChoice.FIRST),
                _trial(1, ("worse", "better"), JudgeChoice.FIRST),
            )
        )
        verdict = JudgeScorer().score(self._case({"kind": "position"}), record.as_text())
        assert verdict.score == 0.0
        assert verdict.detail["swap_consistency"] == 0.0

    def test_a_missing_record_is_a_harness_failure_not_a_judge_failure(self) -> None:
        verdict = JudgeScorer().score(self._case({"kind": "position"}), "not a record")
        assert verdict.score is None
        assert verdict.error_code == "JUDGE_RECORD_MISSING"

    def test_a_case_with_no_expectation_is_unscoreable(self) -> None:
        verdict = JudgeScorer().score(
            BenchmarkCase(case_id="c", ordinal=0, prompt="p"), JudgeRecord().as_text()
        )
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    @pytest.mark.parametrize(
        "expectation",
        [
            {"kind": "unknown_kind"},
            {"kind": "pairwise"},
            {"kind": "verbosity"},
            {"kind": "self_preference"},
            {"kind": "transitivity", "ordering": ["a", "b"]},
        ],
    )
    def test_a_case_that_cannot_distinguish_bias_is_refused(
        self, expectation: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError):
            JudgeExpectation.from_json(expectation)

    def test_an_unknown_stored_choice_reads_back_as_unparseable(self) -> None:
        # A record written by a future build must still be readable as "we could not use this".
        trial = JudgeTrial.from_json({"ordinal": 0, "choice": "something_new"})
        assert trial.choice is JudgeChoice.UNPARSEABLE
