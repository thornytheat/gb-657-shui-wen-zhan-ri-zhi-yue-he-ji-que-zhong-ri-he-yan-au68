"""M8 裁决与月量引擎：版本裁决、零雨/缺测、同版异值、闰月缺日、Decimal、四账。"""
from __future__ import annotations

from decimal import Decimal

from app.modules.m8_correction import engine


def _row(row_id, station, date, value, version=1, kind="measured", run="r1",
         lineno=2, text=None):
    return {"row_id": row_id, "station": station, "date": date, "version": version,
            "kind": kind, "value_norm": None if value is None else Decimal(str(value)),
            "run_id": run, "lineno": lineno, "value_text": text or str(value)}


def test_zero_rain_is_measurement_not_missing():
    decision = engine.arbitrate([_row(1, "S1", "2024-02-01", "0.0")])
    assert len(decision["selected"]) == 1
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["rainfall_mm"] == Decimal("0.0")
    assert monthly["zero_days"] == 1
    assert monthly["observed_days"] == 1


def test_missing_day_is_separate_state_and_not_counted_as_zero():
    row = _row(1, "S1", "2024-02-01", None, kind="missing", text="缺测")
    decision = engine.arbitrate([row])
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["rainfall_mm"] == Decimal("0.0")
    assert monthly["zero_days"] == 0
    assert monthly["missing_reports"] == 1
    assert monthly["observed_days"] == 1


def test_higher_version_wins_regardless_of_order():
    rows = [
        _row(1, "S1", "2024-02-01", "8.0", version=1),
        _row(2, "S1", "2024-02-01", "3.0", version=2),
    ]
    forward = engine.arbitrate(rows)["selected"][0]
    reverse = engine.arbitrate(list(reversed(rows)))["selected"][0]
    assert forward["row_id"] == reverse["row_id"] == 2
    assert forward["value_norm"] == Decimal("3.0")


def test_lower_version_kept_as_duplicate_not_deleted():
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-01", "8.0", version=1),
        _row(2, "S1", "2024-02-01", "3.0", version=2),
    ])
    assert len(decision["selected"]) == 1
    assert len(decision["duplicates"]) == 1
    assert decision["duplicates"][0]["row_id"] == 1


def test_same_version_different_values_isolates_whole_group():
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-01", "3.0", version=2),
        _row(2, "S1", "2024-02-01", "8.0", version=2),
    ])
    assert decision["selected"] == []
    assert len(decision["conflicts"]) == 1
    assert decision["conflicts"][0]["reason_code"] == "Q_CORR_VERSION_CONFLICT"
    # 两条都留作证据，不静默挑一条
    assert {r["row_id"] for r in decision["conflicts"][0]["rows"]} == {1, 2}


def test_same_version_one_missing_one_measured_is_conflict():
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-01", "3.0", version=2),
        _row(2, "S1", "2024-02-01", None, version=2, kind="missing"),
    ])
    assert len(decision["conflicts"]) == 1
    assert decision["selected"] == []


def test_identical_same_version_rows_collapse_to_one():
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-01", "3.0", version=2),
        _row(2, "S1", "2024-02-01", "3.0", version=2),
    ])
    assert len(decision["selected"]) == 1
    assert len(decision["conflicts"]) == 0


def test_leap_february_has_29_days_and_lists_missing_dates():
    decision = engine.arbitrate([_row(1, "S1", "2024-02-29", "0.3")])
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["expected_days"] == 29
    assert monthly["missing_days"] == 28
    assert monthly["missing_dates"][0] == "2024-02-01"
    assert "2024-02-29" not in monthly["missing_dates"]


def test_common_february_has_28_days():
    decision = engine.arbitrate([_row(1, "S1", "2023-02-01", "1.0")])
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["expected_days"] == 28
    assert monthly["missing_days"] == 27


def test_conflict_day_counts_as_isolated_not_missing():
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-10", "1.0", version=1),
        _row(2, "S1", "2024-02-10", "9.0", version=1),
    ])
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["isolated_days"] == 1
    # 隔离日已被覆盖，不能再算进缺日
    assert monthly["missing_days"] == 28
    assert monthly["state"] == "partial"


def test_decimal_sum_is_exact_to_one_tenth():
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-01", "0.1"),
        _row(2, "S1", "2024-02-02", "0.2"),
    ])
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["rainfall_mm"] == Decimal("0.3")


def test_complete_month_state():
    rows = [_row(i, "S1", f"2024-02-{i:02d}", "1.0") for i in range(1, 30)]
    decision = engine.arbitrate(rows)
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["state"] == "complete"
    assert monthly["missing_days"] == 0 and monthly["isolated_days"] == 0


def test_reconcile_balances_and_exposes_delta():
    assert engine.reconcile(10, 7, 2, 1)["balanced"] is True
    assert engine.reconcile(10, 7, 1, 1)["delta"] == 1


def test_selection_keys_on_station_and_date_independently():
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-01", "1.0"),
        _row(2, "S2", "2024-02-01", "2.0"),
    ])
    assert len(decision["selected"]) == 2


def test_leap_missing_dates_are_explicit_and_exclude_observed():
    # 只有 28、29 两天有值（29 是闰日），其余 27 天必须逐一列出
    decision = engine.arbitrate([
        _row(1, "S1", "2024-02-28", "1.0"),
        _row(2, "S1", "2024-02-29", "2.0"),
    ])
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["expected_days"] == 29
    assert monthly["missing_days"] == 27
    assert monthly["missing_dates"][0] == "2024-02-01"
    assert monthly["missing_dates"][-1] == "2024-02-27"
    assert "2024-02-28" not in monthly["missing_dates"]
    assert "2024-02-29" not in monthly["missing_dates"]
    assert monthly["rainfall_mm"] == Decimal("3.0")


def test_non_leap_february_does_not_invent_29th():
    decision = engine.arbitrate([_row(1, "S1", "2023-02-28", "1.0")])
    monthly = engine.aggregate(decision["selected"], decision["conflicts"])[0]
    assert monthly["expected_days"] == 28
    assert monthly["missing_days"] == 27
    assert monthly["state"] == "partial"
    assert all(not d.endswith("-29") for d in monthly["missing_dates"])
