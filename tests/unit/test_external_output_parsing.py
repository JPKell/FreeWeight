"""Every adapter parses recorded output, including malformed and partial, as untrusted input.

Two properties hold for all nine adapters and are asserted uniformly: a parser never raises for
any bytes (the fuzz test feeds each one hostile input), and an unscoreable case becomes a
``None`` score with a reason rather than a fabricated zero (ADR-0016). The per-adapter tests then
pin the actual numbers each tool's real output shape produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from freeweight.external.adapters import ADAPTERS, Adapter, get_adapter
from freeweight.external.adapters.parsing import clamp_unit_score

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "external"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _adapter(key: str) -> Adapter:
    adapter = get_adapter(key)
    assert adapter is not None, key
    return adapter


class TestNoParserEverRaises:
    """The untrusted-input contract: hostile bytes are an outcome, never an exception."""

    HOSTILE = (
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[",
        b"{}",
        b"[1, 2, 3]",
        b'{"results": {}}',
        b"null",
        b'"a string"',
        b'{"samples": [1, 2, {"score": "NaN"}]}',
        b"\x00" * 1000,
    )

    @pytest.mark.parametrize("key", sorted(ADAPTERS))
    @pytest.mark.parametrize("payload", HOSTILE)
    def test_parse_returns_an_outcome_never_raises(self, key: str, payload: bytes) -> None:
        adapter = ADAPTERS[key]
        outcome = adapter.parse(payload)  # must not raise
        # A failed parse is a reported error, and it must carry a reason.
        if not outcome.ok:
            assert outcome.error_code == "EXTERNAL_BENCHMARK_FAILED" or outcome.samples == ()

    @pytest.mark.parametrize("key", sorted(ADAPTERS))
    def test_every_unscored_sample_has_a_reason(self, key: str) -> None:
        """No adapter ever produces a ``None`` score without an error_code (ADR-0016)."""
        adapter = ADAPTERS[key]
        # A record with the right shape but an unscoreable value.
        outcome = adapter.parse(
            b'[{"id": "x", "passed": "maybe", "valid": "maybe", '
            b'"correct": "maybe", "score": 5, "follow_instruction_list": []}]'
        )
        for sample in outcome.samples:
            if sample.score is None:
                assert sample.error_code is not None
                assert sample.error_text


class TestClampUnitScore:
    def test_it_rejects_nan_and_inf(self) -> None:
        assert clamp_unit_score(float("nan")) is None
        assert clamp_unit_score(float("inf")) is None

    def test_it_rejects_out_of_range(self) -> None:
        assert clamp_unit_score(1.5) is None
        assert clamp_unit_score(-0.1) is None

    def test_it_accepts_booleans_as_one_and_zero(self) -> None:
        assert clamp_unit_score(True) == 1.0
        assert clamp_unit_score(False) == 0.0

    def test_it_accepts_a_unit_float(self) -> None:
        assert clamp_unit_score(0.62) == 0.62


class TestLmEvalHarness:
    def test_clean_output_yields_summary_and_samples(self) -> None:
        outcome = _adapter("external.lm_eval_harness").parse(_read("lm_eval_harness.clean.json"))

        assert outcome.ok
        assert outcome.metrics["gsm8k.exact_match"] == 0.62
        assert len(outcome.samples) == 3
        assert outcome.samples[0].score == 1.0

    def test_jsonl_with_a_corrupt_line_is_partial(self) -> None:
        outcome = _adapter("external.lm_eval_harness").parse(_read("lm_eval_harness.jsonl"))

        assert outcome.ok
        assert outcome.partial, "a corrupt line must mark the parse partial"
        assert len(outcome.samples) == 2

    def test_malformed_output_is_a_failure_with_a_reason(self) -> None:
        outcome = _adapter("external.lm_eval_harness").parse(
            _read("lm_eval_harness.malformed.json")
        )

        assert not outcome.ok
        assert outcome.error_code == "EXTERNAL_BENCHMARK_FAILED"

    def test_truncated_output_does_not_crash(self) -> None:
        outcome = _adapter("external.lm_eval_harness").parse(_read("truncated.json"))

        assert not outcome.ok


class TestIFEval:
    def test_prompt_scores_are_the_followed_fraction(self) -> None:
        outcome = _adapter("external.ifeval").parse(_read("ifeval.jsonl"))

        assert outcome.samples[0].score == pytest.approx(2 / 3)
        assert outcome.samples[1].score == 1.0
        # The empty-instruction prompt is unscoreable, not zero.
        assert outcome.samples[2].score is None
        assert outcome.samples[2].error_code == "EXTERNAL_BENCHMARK_FAILED"
        assert outcome.metrics["instruction_level_strict_accuracy"] == pytest.approx(4 / 5)

    def test_non_utf8_output_is_a_failure(self) -> None:
        outcome = _adapter("external.ifeval").parse(_read("ifeval.malformed.json"))

        assert not outcome.ok


class TestEvalPlus:
    def test_base_pass_is_the_case_score_and_fragility_is_reported(self) -> None:
        outcome = _adapter("external.evalplus").parse(_read("evalplus.clean.json"))

        scored = {s.case_id: s.score for s in outcome.samples if s.score is not None}
        assert scored["HumanEval/0"] == 1.0
        assert scored["HumanEval/2"] == 0.0
        # HumanEval/3 has no base_status -> unscored with a reason.
        unscored = [s for s in outcome.samples if s.score is None]
        assert len(unscored) == 1
        assert unscored[0].error_code == "EXTERNAL_BENCHMARK_FAILED"
        # pass@1 = 2 base-passes / 3 scored; fragility = (2 - 1) / 3.
        assert outcome.metrics["pass_at_1"] == pytest.approx(2 / 3)
        assert outcome.metrics["fragility"] == pytest.approx(1 / 3)

    def test_it_declares_it_needs_a_sandbox(self) -> None:
        assert _adapter("external.evalplus").manifest.requires_sandbox is True

    def test_malformed_output_is_a_failure(self) -> None:
        outcome = _adapter("external.evalplus").parse(_read("evalplus.malformed.json"))

        assert not outcome.ok


class TestCruxEval:
    def test_pass_fail_and_a_bad_type_is_unscored(self) -> None:
        outcome = _adapter("external.cruxeval").parse(_read("cruxeval.jsonl"))

        assert outcome.samples[0].score == 1.0
        assert outcome.samples[1].score == 0.0
        assert outcome.samples[2].score is None  # "passed": "yes" is not a boolean
        assert outcome.samples[2].error_code == "EXTERNAL_BENCHMARK_FAILED"

    def test_it_needs_a_sandbox(self) -> None:
        assert _adapter("external.cruxeval").manifest.requires_sandbox is True


class TestBfcl:
    def test_valid_flags_become_accuracy(self) -> None:
        outcome = _adapter("external.bfcl").parse(_read("bfcl.clean.json"))

        assert outcome.metrics["overall_accuracy"] == pytest.approx(2 / 3)
        assert outcome.metrics["accuracy.simple"] == 1.0

    def test_a_missing_valid_field_is_unscored_but_the_list_parses(self) -> None:
        outcome = _adapter("external.bfcl").parse(_read("bfcl.partial.json"))

        assert len(outcome.samples) == 2
        assert outcome.samples[1].score is None
        assert outcome.samples[1].error_code == "EXTERNAL_BENCHMARK_FAILED"


class TestRuler:
    def test_per_length_scores_and_an_out_of_range_is_unscored(self) -> None:
        outcome = _adapter("external.ruler").parse(_read("ruler.jsonl"))

        assert outcome.samples[0].score == 1.0
        assert outcome.samples[2].score is None  # 1.5 is out of range
        assert outcome.metrics["score.4096"] == 1.0
        assert outcome.metrics["score.8192"] == 0.5


class TestJudgeBench:
    def test_preference_accuracy(self) -> None:
        outcome = _adapter("external.judgebench").parse(_read("judgebench.clean.json"))

        assert outcome.metrics["preference_accuracy"] == 0.5


class TestLlmBar:
    def test_per_subset_accuracy_isolates_the_adversarial_split(self) -> None:
        outcome = _adapter("external.llmbar").parse(_read("llmbar.clean.json"))

        assert outcome.metrics["accuracy.Natural"] == 1.0
        assert outcome.metrics["accuracy.Adversarial_GPTInst"] == 0.5


class TestCriticBench:
    def test_per_dimension_scores(self) -> None:
        outcome = _adapter("external.criticbench").parse(_read("criticbench.jsonl"))

        assert outcome.metrics["score.generation"] == pytest.approx(0.8)
        assert outcome.metrics["score.critique"] == pytest.approx(0.6)
        assert outcome.metrics["score.correction"] == pytest.approx(0.9)
