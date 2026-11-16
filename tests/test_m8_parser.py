"""M8 更正 CSV 解析：编码回退、表头别名、三态、坏行隔离、文件级原因码。"""
from __future__ import annotations

from decimal import Decimal

from app.modules.m8_correction import parser


def test_bom_and_standard_header():
    result = parser.parse(
        "station,date,rainfall_mm,version\nS1,2024-02-01,1.5,1\n".encode("utf-8-sig"))
    assert result.file_error is None
    assert result.bom_stripped is True
    row = result.rows[0]
    assert row.station == "S1" and row.date == "2024-02-01"
    assert row.value_norm == Decimal("1.5") and row.version == 1
    assert row.lineno == 2


def test_gbk_chinese_header_fallback():
    data = "站号,日期,雨量,版本\nS1,2024/02/01,2.5,1\n".encode("gb18030")
    result = parser.parse(data)
    assert result.fallback is True
    assert result.rows[0].station == "S1"
    assert result.rows[0].date == "2024-02-01"
    assert result.rows[0].value_norm == Decimal("2.5")


def test_zero_rain_is_valid_measured():
    result = parser.parse(
        b"station,date,rainfall_mm,version\nS1,2024-02-01,0.0,1\n")
    row = result.rows[0]
    assert row.kind == "measured" and row.value_norm == Decimal("0.0")
    assert row.reason_code == "K_CORR_ZERO_RAIN"


def test_missing_tokens_become_missing_kind():
    for token in ("缺测", "-", "N/A", ""):
        line = f"S1,2024-02-01,{token},1\n"
        result = parser.parse(
            ("station,date,rainfall_mm,version\n" + line).encode("utf-8"))
        row = result.rows[0]
        assert row.kind == "missing" and row.value_norm is None
        assert row.reason_code == "Q_CORR_MISSING"


def test_bad_value_and_negative_are_isolated():
    data = (b"station,date,rainfall_mm,version\n"
            b"S1,2024-02-01,abc,1\n"
            b"S1,2024-02-02,-3,1\n")
    result = parser.parse(data)
    assert result.rows == []
    codes = {r.reason_code for r in result.isolated}
    assert codes == {"Q_CORR_BAD_VALUE", "Q_CORR_NEGATIVE"}


def test_bad_date_isolated_with_physical_lineno():
    data = (b"station,date,rainfall_mm,version\n"
            b"S1,not-a-date,1.0,1\n")
    result = parser.parse(data)
    assert result.isolated[0].reason_code == "Q_CORR_BAD_DATE"
    assert result.isolated[0].lineno == 2


def test_column_count_mismatch_isolated():
    data = b"station,date,rainfall_mm,version\nS1,2024-02-01,1.0\n"
    result = parser.parse(data)
    assert result.isolated[0].reason_code == "Q_COLUMN_COUNT"


def test_blank_lines_are_skipped_but_lineno_stays_physical():
    data = (b"station,date,rainfall_mm,version\n"
            b"\n"
            b"S1,2024-02-01,1.0,1\n")
    result = parser.parse(data)
    assert len(result.rows) == 1
    assert result.rows[0].lineno == 3


def test_empty_file_returns_distinct_code():
    assert parser.parse(b"").file_error == "E_CORR_EMPTY"
    assert parser.parse(b"\n\n  \n").file_error == "E_CORR_EMPTY"


def test_header_missing_column_returns_code():
    result = parser.parse(b"station,date\nS1,2024-02-01\n")
    assert result.file_error == "E_CORR_HEADER"


def test_default_version_is_one_and_bad_version_isolated():
    result = parser.parse(b"station,date,rainfall_mm\nS1,2024-02-01,1.0\n")
    assert result.rows[0].version == 1
    bad = parser.parse(
        b"station,date,rainfall_mm,version\nS1,2024-02-01,1.0,vx\n")
    assert bad.isolated[0].reason_code == "E_CORR_VERSION"


def test_thousands_separator_normalized():
    result = parser.parse(
        b"station,date,rainfall_mm,version\nS1,2024-02-01,\"1,212.5\",1\n")
    assert result.rows[0].value_norm == Decimal("1212.5")


def test_input_count_covers_valid_and_isolated():
    data = (b"station,date,rainfall_mm,version\n"
            b"S1,2024-02-01,1.0,1\n"
            b"S1,bad,1.0,1\n")
    result = parser.parse(data)
    assert result.input_count == 2
