"""Timestamps: tz-aware in Python, naive UTC on the wire (SQL Server DATETIME2 rejects '+00:00' strings)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import mssql, sqlite

from app.models import UTCDateTime


def test_bind_strips_offset_and_converts_to_utc():
    t = UTCDateTime()
    pst = datetime(2026, 10, 10, 9, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-7)))
    out = t.process_bind_param(pst, mssql.dialect())
    assert out.tzinfo is None and out == datetime(2026, 10, 10, 16, 0, 0, 123456)


def test_result_is_utc_aware():
    out = UTCDateTime().process_result_value(datetime(2026, 1, 1, 12, 0), sqlite.dialect())
    assert out.tzinfo == timezone.utc


def test_mssql_uses_datetime2_6():
    impl = UTCDateTime().load_dialect_impl(mssql.dialect())
    assert isinstance(impl, mssql.DATETIME2) and impl.precision == 6
