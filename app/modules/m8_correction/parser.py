"""更正日值 CSV 解析：编码回退、表头别名、零雨/缺测三态、行号保真。

只做「逐行能不能成为一条日值」的形态校验；同版异值这类跨行裁决在
arbitrate 里完成，保证解析与裁决可分别测试、可续跑重放。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ... import config, reasons
from ..m1_ingest.decode import decode_bytes

# 规范列 → 可接受表头（兼顾 GBK 中文导出与英文表头）
HEADER_ALIASES = {
    "station": ("station", "站号", "站点", "水文站", "station_id", "测站"),
    "date": ("date", "日期", "观测日期", "day"),
    "rainfall": ("rainfall", "rainfall_mm", "雨量", "日雨量", "降雨量", "降水量"),
    "version": ("version", "版本", "数据版本", "ver", "版本号"),
}
REQUIRED = ("station", "date", "rainfall")

MISSING_TOKENS = frozenset(config.SENTINELS) | {"缺测", "缺", "m", "M"}


@dataclass
class ParsedRow:
    lineno: int
    station: str
    date: str
    version: int | None
    value_text: str
    value_norm: Decimal | None
    kind: str                 # measured / missing
    row_status: str           # valid / isolated
    reason_code: str | None = None
    note: str = ""


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    isolated: list[ParsedRow] = field(default_factory=list)
    encoding: str = "utf-8"
    bom_stripped: bool = False
    fallback: bool = False
    file_error: str | None = None
    header: list[str] = field(default_factory=list)

    @property
    def input_count(self) -> int:
        return len(self.rows) + len(self.isolated)


def _map_header(header: list[str]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, raw in enumerate(header):
        name = (raw or "").strip().lstrip("﻿").strip()
        for canonical, aliases in HEADER_ALIASES.items():
            if name.lower() in {a.lower() for a in aliases} and canonical not in positions:
                positions[canonical] = index
    return positions


def _parse_date(text: str) -> str | None:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_version(text: str) -> tuple[int | None, str | None]:
    text = (text or "").strip()
    if text == "":
        return 1, None                       # 缺省按首版处理
    try:
        number = int(text)
    except ValueError:
        try:
            number = int(Decimal(text))
        except (InvalidOperation, ValueError):
            return None, "E_CORR_VERSION"
    if number < 1:
        return None, "E_CORR_VERSION"
    return number, None


def _parse_rainfall(text: str) -> tuple[Decimal | None, str, str | None]:
    """返回 (数值, 类型 measured/missing, 隔离原因)。零雨是合法实测。"""
    raw = (text or "").strip()
    if raw.lower() in MISSING_TOKENS:
        return None, "missing", None
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None, "measured", "Q_CORR_BAD_VALUE"
    if value < 0:
        return None, "measured", "Q_CORR_NEGATIVE"
    return value, "measured", None


def parse(data: bytes) -> ParseResult:
    result = ParseResult()
    decoded = decode_bytes(data)
    result.encoding = decoded["encoding"]
    result.bom_stripped = decoded["bom_stripped"]
    result.fallback = decoded["fallback"]

    reader = csv.reader(io.StringIO(decoded["text"], newline=""))
    records = [(reader.line_num, list(fields)) for fields in reader]
    records = [(lineno, fields) for lineno, fields in records
               if any(c.strip() for c in fields)]
    if not records:
        result.file_error = "E_CORR_EMPTY"
        return result

    header_lineno, header = records[0]
    result.header = header
    positions = _map_header(header)
    missing_cols = [col for col in REQUIRED if col not in positions]
    if missing_cols:
        result.file_error = "E_CORR_HEADER"
        return result

    for lineno, fields in records[1:]:
        if len(fields) != len(header):
            result.isolated.append(ParsedRow(
                lineno=lineno, station="", date="", version=None,
                value_text=",".join(fields), value_norm=None, kind="measured",
                row_status="isolated", reason_code="Q_COLUMN_COUNT",
                note=f"列数 {len(fields)} ≠ 表头 {len(header)}"))
            continue
        station = fields[positions["station"]].strip()
        date_text = fields[positions["date"]].strip()
        rain_text = fields[positions["rainfall"]].strip()
        version_text = (fields[positions["version"]].strip()
                        if "version" in positions else "")
        date = _parse_date(date_text)
        if not station or date is None:
            result.isolated.append(ParsedRow(
                lineno=lineno, station=station, date=date_text, version=None,
                value_text=rain_text, value_norm=None, kind="measured",
                row_status="isolated", reason_code="Q_CORR_BAD_DATE"))
            continue
        version, version_err = _parse_version(version_text)
        if version_err:
            result.isolated.append(ParsedRow(
                lineno=lineno, station=station, date=date, version=None,
                value_text=rain_text, value_norm=None, kind="measured",
                row_status="isolated", reason_code=version_err))
            continue
        value, kind, value_err = _parse_rainfall(rain_text)
        if value_err:
            result.isolated.append(ParsedRow(
                lineno=lineno, station=station, date=date, version=version,
                value_text=rain_text, value_norm=None, kind="measured",
                row_status="isolated", reason_code=value_err))
            continue
        reason = None
        note = ""
        if kind == "missing":
            reason = "Q_CORR_MISSING"
            note = reasons.label(reason)
        elif value == 0:
            reason = "K_CORR_ZERO_RAIN"
            note = reasons.label(reason)
        result.rows.append(ParsedRow(
            lineno=lineno, station=station, date=date, version=version,
            value_text=rain_text, value_norm=value, kind=kind,
            row_status="valid", reason_code=reason, note=note))

    return result
