"""Unit tests for freeweight.infrastructure.db.types."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from baseaicore import ValidationError
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from freeweight.infrastructure.db.types import (
    PortableJSON,
    UtcDateTime,
    measurement_columns,
    ulid_primary_key,
)


def _table_with(name: str, column: Column[Any]) -> tuple[MetaData, Table]:
    metadata = MetaData()
    table = Table(name, metadata, Column("id", Integer, primary_key=True), column)
    return metadata, table


def test_utc_datetime_round_trips_a_timezone_aware_value() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata, table = _table_with("t", Column("stamp", UtcDateTime))
    metadata.create_all(engine)
    original = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.execute(table.insert().values(id=1, stamp=original))
        session.commit()
        result = session.execute(select(table.c.stamp)).scalar_one()

    assert result == original
    assert result.tzinfo is not None
    assert result.tzinfo.utcoffset(result).total_seconds() == 0


def test_utc_datetime_normalizes_a_non_utc_timezone_to_utc() -> None:
    from datetime import timedelta, timezone

    engine = create_engine("sqlite:///:memory:")
    metadata, table = _table_with("t", Column("stamp", UtcDateTime))
    metadata.create_all(engine)
    plus_five = timezone(timedelta(hours=5))
    original = datetime(2026, 8, 26, 17, 0, 0, tzinfo=plus_five)

    with Session(engine) as session:
        session.execute(table.insert().values(id=1, stamp=original))
        session.commit()
        result = session.execute(select(table.c.stamp)).scalar_one()

    assert result == datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


def test_utc_datetime_rejects_a_naive_value() -> None:
    """SQLAlchemy wraps a bind-processor exception in ``StatementError``; the ``ValidationError``
    this type actually raises is chained underneath as the cause, not raised bare."""
    from sqlalchemy.exc import StatementError

    engine = create_engine("sqlite:///:memory:")
    metadata, table = _table_with("t", Column("stamp", UtcDateTime))
    metadata.create_all(engine)
    naive = datetime(2026, 8, 26, 12, 0, 0)  # noqa: DTZ001 — the value under test

    with Session(engine) as session, pytest.raises(StatementError) as excinfo:
        session.execute(table.insert().values(id=1, stamp=naive))
        session.commit()

    assert isinstance(excinfo.value.orig, ValidationError)


def test_utc_datetime_round_trips_none() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata, table = _table_with("t", Column("stamp", UtcDateTime, nullable=True))
    metadata.create_all(engine)

    with Session(engine) as session:
        session.execute(table.insert().values(id=1, stamp=None))
        session.commit()
        result = session.execute(select(table.c.stamp)).scalar_one()

    assert result is None


def test_portable_json_round_trips_nested_unicode_structures() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata, table = _table_with("t", Column("payload", PortableJSON))
    metadata.create_all(engine)
    payload = {
        "gpus": [{"name": "RTX 5060 Ti — 日本語", "vram_total_bytes": 17179869184}],
        "n": None,
    }

    with Session(engine) as session:
        session.execute(table.insert().values(id=1, payload=payload))
        session.commit()
        result = session.execute(select(table.c.payload)).scalar_one()

    assert result == payload


def test_portable_json_round_trips_a_list() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata, table = _table_with("t", Column("payload", PortableJSON))
    metadata.create_all(engine)

    with Session(engine) as session:
        session.execute(table.insert().values(id=1, payload=[1, "two", 3.0, None, True]))
        session.commit()
        result = session.execute(select(table.c.payload)).scalar_one()

    assert result == [1, "two", 3.0, None, True]


class _WidgetsBase(DeclarativeBase):
    """A throwaway declarative base, local to this test module."""


class _Widget(_WidgetsBase):
    __tablename__ = "widgets"
    id: Mapped[str] = ulid_primary_key()
    name: Mapped[str] = mapped_column(String)


def test_ulid_primary_key_defaults_when_not_supplied() -> None:
    engine = create_engine("sqlite:///:memory:")
    _WidgetsBase.metadata.create_all(engine)

    with Session(engine) as session:
        widget = _Widget(name="drill")
        session.add(widget)
        session.commit()

        assert widget.id is not None
        assert len(widget.id) == 26
        assert widget.id.startswith(widget.id[:1])  # plain str, no lazy-load surprises


class _GpuReading(_WidgetsBase):
    __tablename__ = "gpu_readings"
    id: Mapped[str] = ulid_primary_key()
    vram_total_bytes, vram_total_bytes_unavailable_reason = measurement_columns("vram_total_bytes")


def test_measurement_columns_produces_the_fixed_name_pair() -> None:
    engine = create_engine("sqlite:///:memory:")
    _WidgetsBase.metadata.create_all(engine)
    columns = {column.name for column in _GpuReading.__table__.columns}

    assert {"vram_total_bytes", "vram_total_bytes_unavailable_reason"} <= columns

    with Session(engine) as session:
        reading = _GpuReading(vram_total_bytes=None, vram_total_bytes_unavailable_reason="no_gpu")
        session.add(reading)
        session.commit()
        session.refresh(reading)

        assert reading.vram_total_bytes is None
        assert reading.vram_total_bytes_unavailable_reason == "no_gpu"
