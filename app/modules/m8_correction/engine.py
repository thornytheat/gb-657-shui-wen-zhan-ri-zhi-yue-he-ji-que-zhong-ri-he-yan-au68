"""版本裁决与月量引擎：纯函数，不碰数据库与 HTTP。

规则（对应需求）：
- 按 (站点, 日期) 分组，最高版本裁决，版本相同不以后写为准；
- 同版异值（含一实测一缺测）整组隔离，不猜哪条为准；
- 零雨是合法实测参与合计，缺测是三态之一，不按 0 计入；
- 月合计 Decimal 求和后量化到 0.1 mm；
- 缺日按当地自然日日历逐日核对，闰月自动 29 天并显式列出缺日。
"""
from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal

QUANT = Decimal("0.1")


def _value_signature(row: dict) -> str:
    if row["kind"] == "missing":
        return "∅"
    return str(row["value_norm"])


def arbitrate(rows: list[dict]) -> dict:
    """对全部已采信日值（跨多次更正）按站点日期裁决。

    入参行需含 row_id、station、date、version、kind、value_norm。
    返回 selected（每天一条）、duplicates（低版本留存）、conflicts（同版异值）。
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["station"], row["date"]), []).append(row)

    selected: list[dict] = []
    duplicates: list[dict] = []
    conflicts: list[dict] = []
    for (station, day), day_rows in sorted(grouped.items()):
        ordered = sorted(day_rows, key=lambda r: (int(r["version"]), r["row_id"]))
        top_version = max(int(r["version"]) for r in ordered)
        top = [r for r in ordered if int(r["version"]) == top_version]
        signatures = {_value_signature(r) for r in top}
        if len(signatures) > 1:
            conflicts.append({
                "station": station, "date": day, "version": top_version,
                "reason_code": "Q_CORR_VERSION_CONFLICT",
                "rows": [{
                    "row_id": r["row_id"], "version": r["version"],
                    "kind": r["kind"], "value_text": r["value_text"],
                    "run_id": r["run_id"], "lineno": r["lineno"],
                } for r in top],
            })
            # 同版异值：该天所有行都不采信，全部留存为隔离证据
            duplicates.extend(ordered)
            continue
        winner = top[0]
        selected.append(winner)
        duplicates.extend(r for r in ordered if r["row_id"] != winner["row_id"])
    return {"selected": selected, "duplicates": duplicates, "conflicts": conflicts}


def month_of(date_text: str) -> str:
    return date_text[:7]


def expected_days(month: str) -> int:
    year, mon = (int(part) for part in month.split("-"))
    return calendar.monthrange(year, mon)[1]


def aggregate(selected: list[dict], conflicts: list[dict]) -> list[dict]:
    """按 (站点, 月) 汇总；缺日 = 自然日 − 采信日 − 隔离日，并列出具体缺日。"""
    # 每站每月：采信日、零雨、缺测标记、合计
    buckets: dict[tuple[str, str], dict] = {}
    conflict_days: dict[tuple[str, str], set[str]] = {}
    for conflict in conflicts:
        key = (conflict["station"], month_of(conflict["date"]))
        conflict_days.setdefault(key, set()).add(conflict["date"])

    for row in selected:
        key = (row["station"], month_of(row["date"]))
        bucket = buckets.setdefault(key, {
            "station": key[0], "month": key[1], "rainfall_mm": Decimal("0"),
            "observed_days": 0, "zero_days": 0, "missing_reports": 0,
            "dates": set(),
        })
        bucket["observed_days"] += 1
        bucket["dates"].add(row["date"])
        if row["kind"] == "missing":
            bucket["missing_reports"] += 1
        else:
            if row["value_norm"] == 0:
                bucket["zero_days"] += 1
            bucket["rainfall_mm"] += row["value_norm"]

    # 冲突所在月份即使没有采信行也要建桶，保证隔离数对得上
    for key, days in conflict_days.items():
        buckets.setdefault(key, {
            "station": key[0], "month": key[1], "rainfall_mm": Decimal("0"),
            "observed_days": 0, "zero_days": 0, "missing_reports": 0,
            "dates": set(),
        })

    output: list[dict] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        station, month = key
        total_days = expected_days(month)
        isolated = sorted(conflict_days.get(key, set()))
        covered = bucket["dates"] | set(isolated)
        year, mon = (int(part) for part in month.split("-"))
        missing_dates = [
            dt.date(year, mon, d).isoformat()
            for d in range(1, total_days + 1)
            if dt.date(year, mon, d).isoformat() not in covered
        ]
        missing_count = total_days - len(covered)
        state = "complete" if not missing_dates and not isolated else "partial"
        output.append({
            "station": station,
            "month": month,
            "rainfall_mm": bucket["rainfall_mm"].quantize(QUANT),
            "observed_days": bucket["observed_days"],
            "zero_days": bucket["zero_days"],
            "missing_reports": bucket["missing_reports"],
            "missing_days": missing_count,
            "isolated_days": len(isolated),
            "expected_days": total_days,
            "missing_dates": missing_dates,
            "state": state,
        })
    return output


def reconcile(source: int, accepted: int, duplicates: int, isolated: int) -> dict:
    """四账：输入 = 采纳 + 重复 + 隔离（隔离含同版异值与坏行）。"""
    delta = source - accepted - duplicates - isolated
    return {"source": source, "accepted": accepted, "duplicates": duplicates,
            "isolated": isolated, "delta": delta, "balanced": delta == 0}
