"""A disabled model is not measured, and the refusal says who disabled it (ADR-0118)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from baseaicore import ValidationError

from freeweight.config import ExecutionSettings
from freeweight.services.models import list_models_with_latest_descriptor, set_model_enabled
from freeweight.services.runs import ExecutionConfig, create_run

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import RunEnvironment


@pytest.fixture
def environment(run_environment: Callable[..., RunEnvironment]) -> RunEnvironment:
    return run_environment()


def _execution() -> ExecutionConfig:
    return ExecutionConfig.resolve(
        ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0), measured_repetitions=1
    )


def test_a_disabled_model_is_refused_by_name_and_not_measured(
    environment: RunEnvironment,
) -> None:
    model_id = list_models_with_latest_descriptor(environment.database)[0].id
    set_model_enabled(environment.database, model_ref=model_id, enabled=False)

    with pytest.raises(ValidationError) as caught:
        create_run(
            environment.database,
            environment.provider,
            environment.collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key="native.echo",
            execution=_execution(),
        )
    assert "disabled" in str(caught.value)


def test_re_enabling_restores_exactly_what_was_there(environment: RunEnvironment) -> None:
    model_id = list_models_with_latest_descriptor(environment.database)[0].id
    set_model_enabled(environment.database, model_ref=model_id, enabled=False)
    set_model_enabled(environment.database, model_ref=model_id, enabled=True)
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=_execution(),
    )
    assert summary is not None
    assert list_models_with_latest_descriptor(environment.database)[0].enabled is True
