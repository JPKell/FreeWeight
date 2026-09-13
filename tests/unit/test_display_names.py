"""What a model is called on a page, when the provider's own name is not unique (row WX7).

The rule is a property of the *list*: a name is ambiguous because another model carries it, which
no single row can know. These tests are the reason the computation does not live in a template.
"""

from __future__ import annotations

from dataclasses import dataclass

from freeweight.services.models import display_names


@dataclass(frozen=True)
class _Row:
    canonical_id: str
    provider_model_name: str
    enabled: bool = True


def test_a_unique_name_is_the_providers_own() -> None:
    """The short name is what an operator recognizes, so it is the default."""
    rows = [
        _Row("ollama/qwen3:8b@sha256:aa", "qwen3:8b"),
        _Row("ollama/gemma:2b@sha256:bb", "gemma:2b"),
    ]

    assert display_names(rows) == {
        "ollama/qwen3:8b@sha256:aa": "qwen3:8b",
        "ollama/gemma:2b@sha256:bb": "gemma:2b",
    }


def test_a_shared_name_falls_back_to_the_canonical_id_for_both() -> None:
    """A name that names two measurable subjects names neither, so neither row may use it."""
    rows = [
        _Row("ollama/qwen3:8b@sha256:aa", "qwen3:8b"),
        _Row("llamacpp/qwen3:8b@sha256:bb", "qwen3:8b"),
    ]

    names = display_names(rows)

    assert names == {
        "ollama/qwen3:8b@sha256:aa": "ollama/qwen3:8b@sha256:aa",
        "llamacpp/qwen3:8b@sha256:bb": "llamacpp/qwen3:8b@sha256:bb",
    }


def test_only_enabled_models_make_a_name_ambiguous() -> None:
    """A disabled model cannot be measured, so it is not one of the things the name could mean."""
    rows = [
        _Row("ollama/qwen3:8b@sha256:aa", "qwen3:8b"),
        _Row("llamacpp/qwen3:8b@sha256:bb", "qwen3:8b", enabled=False),
    ]

    assert display_names(rows)["ollama/qwen3:8b@sha256:aa"] == "qwen3:8b"


def test_a_disabled_model_still_shows_the_ambiguous_form() -> None:
    """Two enabled holders make the short name ambiguous for every row carrying it."""
    rows = [
        _Row("a/qwen3:8b@sha256:aa", "qwen3:8b"),
        _Row("b/qwen3:8b@sha256:bb", "qwen3:8b"),
        _Row("c/qwen3:8b@sha256:cc", "qwen3:8b", enabled=False),
    ]

    assert display_names(rows)["c/qwen3:8b@sha256:cc"] == "c/qwen3:8b@sha256:cc"


def test_an_empty_list_has_no_names() -> None:
    """No models is not an error and not a name."""
    assert display_names([]) == {}
