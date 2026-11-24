"""Turning source values into a column's type without failing a whole read.

Every connector builds its record batches through `rows_to_batch` (tabular.py),
which hands the values to a `Conversion`. A value that cannot become the
column's type is stored as NULL and counted, per column and reason, with up to
five sample raw values. A refresh then reports what it changed instead of
failing on one bad cell, and the report is kept with the snapshot.

Invalid dates, which legacy databases allow (0000-00-00, 2020-00-00, 2020-05-00,
2020-02-30), follow the connection's `invalid_dates` setting:

- "null" (the default): stored as NULL and counted;
- "text": the connection's date and time columns are read as text, so such
  values stay as the source has them (a column can be set back to a date on
  the dataset page);
- "fail": the refresh stops with a message that names the table, the column,
  its source type and the value.

The same Conversion applies a dataset's column type overrides (with a date
format if one is set) to the batches after the connector, and suggests types
for text columns that consistently hold dates, numbers or true/false values.
A suggestion is never applied on its own.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from opsis.connectors.base import ColumnSpec, ColumnType, ConnectorError

INVALID_DATES = ("null", "text", "fail")
MAX_SAMPLES = 5
SUGGEST_SAMPLE = 1000  # text values looked at per column to suggest a type
SUGGEST_MINIMUM = 5  # fewer values than this are too few to suggest anything
INT_MIN, INT_MAX = -(2**63), 2**63 - 1
DECIMAL_DIGITS = 38  # what Arrow's decimal128 (and DuckDB) can hold

# A dataset's type overrides, in the words the dataset page uses.
OVERRIDE_TYPES = {
    "text": ColumnType.STRING,
    "integer": ColumnType.INTEGER,
    "decimal": ColumnType.FLOAT,  # a number with a fractional part, as in a spreadsheet
    "date": ColumnType.DATE,
    "datetime": ColumnType.TIMESTAMP,
    "boolean": ColumnType.BOOLEAN,
}

TRUE_WORDS = frozenset({"true", "t", "yes", "y", "on", "1"})
FALSE_WORDS = frozenset({"false", "f", "no", "n", "off", "0"})
# Placeholders people type for "no value". A column is still "all numbers" with them.
EMPTY_WORDS = frozenset({"null", "none", "n/a", "na", "nil", "-", "--", "?", "#n/a", "nan"})

_INTEGER = re.compile(r"[+-]?\d+")
_GROUPED = re.compile(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?")  # 1,234 or 1,234.56
_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_DATE_SHAPE = re.compile(r"\s*(\d{4,})-(\d{1,2})-(\d{1,2})")
_DIGITS = re.compile(r"\d")

DATE_FORMATS = ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y")
DATETIME_FORMATS = (
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)


def invalid_date(text: str) -> str | None:
    """Why a value written as a date cannot be one; None when it can be (or is
    not written as a date at all)."""
    lowered = text.strip().lower()
    if lowered in ("infinity", "-infinity", "+infinity"):
        return "infinite date"
    if lowered.endswith(" bc"):
        return "date before year 1"
    digits = _DIGITS.findall(text)
    if len(digits) >= 6 and set(digits) == {"0"}:
        return "zero date"  # 0000-00-00, 00/00/0000, 0000-00-00 00:00:00
    shaped = _DATE_SHAPE.match(text)
    if not shaped:
        return None
    year, month, day = (int(part) for part in shaped.groups())
    if year > 9999:
        return "year after 9999"
    if year == 0 or month == 0 or day == 0:
        return "partial date (zero year, month or day)"
    try:
        date(year, month, day)
    except ValueError:
        return "impossible date"
    return None


def _sample(value: Any) -> str:
    text = value if isinstance(value, str) else as_text(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _duration(value: timedelta) -> str:
    """A TIME value as MySQL writes it: [-]HH:MM:SS, hours beyond 24 allowed."""
    sign = "-" if value < timedelta(0) else ""
    value = abs(value)
    total = value.days * 86400 + value.seconds
    text = f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return text + (f".{value.microseconds:06d}" if value.microseconds else "")


def as_text(value: Any) -> str:
    """Any source value as text (for text columns)."""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + raw.hex()  # binary data: shown, not mangled
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return _duration(value)
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, default=str)
    return str(value)


class _Null(Exception):
    """A value that cannot become the column's type: why, for the report."""

    def __init__(self, reason: str, invalid_date: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.invalid_date = invalid_date


@dataclass
class _Issues:
    column: str
    type: str
    source_type: str | None
    count: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    samples: list[str] = field(default_factory=list)


def arrow_type(column: ColumnSpec) -> pa.DataType:
    match column.type:
        case ColumnType.INTEGER:
            return pa.int64()
        case ColumnType.FLOAT:
            return pa.float64()
        case ColumnType.DECIMAL:
            # Without a known scale a decimal cannot be stored exactly.
            return pa.float64() if column.scale is None else pa.decimal128(38, column.scale)
        case ColumnType.BOOLEAN:
            return pa.bool_()
        case ColumnType.DATE:
            return pa.date32()
        case ColumnType.TIMESTAMP:
            return pa.timestamp("us", tz="UTC") if column.tz_aware else pa.timestamp("us")
        case _:
            return pa.string()


_ARROW_ERRORS = (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError, ValueError, TypeError)


def _fits(column: ColumnSpec, seen: pa.DataType) -> bool:
    """Whether values Arrow inferred as `seen` are exactly what the column holds."""
    match column.type:
        case ColumnType.STRING | ColumnType.JSON:
            return bool(pa.types.is_string(seen) or pa.types.is_large_string(seen))
        case ColumnType.INTEGER:
            return bool(pa.types.is_signed_integer(seen))
        case ColumnType.FLOAT:
            return bool(
                pa.types.is_floating(seen)
                or pa.types.is_signed_integer(seen)
                or pa.types.is_decimal(seen)
            )
        case ColumnType.DECIMAL:
            if pa.types.is_signed_integer(seen) or pa.types.is_decimal(seen):
                return True
            return column.scale is None and bool(pa.types.is_floating(seen))
        case ColumnType.BOOLEAN:
            return bool(pa.types.is_boolean(seen))
        case ColumnType.DATE:
            return bool(pa.types.is_date32(seen))
        case ColumnType.TIMESTAMP:
            # An offset on a wall-clock column is dropped, not converted: one by one.
            return bool(pa.types.is_timestamp(seen) and (column.tz_aware or seen.tz is None))
    return False


def _fast(values: list[Any], column: ColumnSpec, kind: pa.DataType) -> pa.Array | None:
    """The values as an Arrow array when they already are the column's type, which
    is nearly always: Arrow then does the work. None means convert one by one.

    Arrow infers the type first, because given a type it can quietly bend a
    value (1.5 into an integer column becomes 1); only exact matches pass here.
    """
    try:
        found = pa.array(values)
        if pa.types.is_null(found.type):
            return pa.nulls(len(values), type=kind)
        if column.type == ColumnType.BOOLEAN and pa.types.is_signed_integer(found.type):
            return pc.not_equal(found, 0)  # MySQL TINYINT(1): nonzero is true, as one by one
        # A safe cast: it raises rather than lose data (a decimal's extra digits).
        return found.cast(kind) if _fits(column, found.type) else None
    except _ARROW_ERRORS:
        return None


class Conversion:
    """Conversion for one read: the invalid-date choice, and what was changed."""

    def __init__(self, *, table: str = "", invalid_dates: str = "null") -> None:
        self.table = table
        self.invalid_dates = invalid_dates if invalid_dates in INVALID_DATES else "null"
        self._issues: dict[str, _Issues] = {}
        self.source_types: dict[str, str] = {}
        self.text_dates: list[str] = []
        self.overrides: dict[str, dict[str, Any]] = {}
        self._texts: dict[str, list[str]] = {}

    # ---- columns -----------------------------------------------------------------------

    def effective(self, column: ColumnSpec) -> ColumnSpec:
        """The column as it will be stored: date columns become text when chosen."""
        if self.invalid_dates == "text" and column.type in (ColumnType.DATE, ColumnType.TIMESTAMP):
            if column.name not in self.text_dates:
                self.text_dates.append(column.name)
            return replace(column, type=ColumnType.STRING, tz_aware=False)
        return column

    def note(self, columns: Sequence[ColumnSpec]) -> None:
        """Remember each column's source type, for the report and error messages."""
        for column in columns:
            self.source_types.setdefault(column.name, column.source_type or column.type.value)

    # ---- values ------------------------------------------------------------------------

    def batch(self, columns: Sequence[ColumnSpec], rows: Sequence[Sequence[Any]]) -> pa.RecordBatch:
        """A typed record batch from row tuples. An empty `rows` gives an empty batch."""
        self.note(columns)
        stored = [self.effective(column) for column in columns]
        arrays = [self.values([row[i] for row in rows], c) for i, c in enumerate(stored)]
        schema = pa.schema([pa.field(c.name, arrow_type(c)) for c in stored])
        return pa.RecordBatch.from_arrays(arrays, schema=schema)

    def values(
        self, raw: list[Any], column: ColumnSpec, date_format: str | None = None
    ) -> pa.Array:
        """One column's values as an Arrow array of its type."""
        kind = arrow_type(column)
        try:
            if date_format is None:
                fast = _fast(raw, column, kind)
                if fast is not None:
                    return fast
            convert = self.converter(column, date_format)
            return self.array([convert(value) for value in raw], column)
        except ConnectorError:
            raise
        except Exception as exc:  # a bug here must still say where it happened
            sample = next((v for v in raw if v is not None), None)
            raise ConnectorError(
                f"Column {column.name!r}{self._of()} ({self._source(column)}) could not be read, "
                f"for example {_sample(sample)!r}: {exc}"
            ) from exc

    def converter(self, column: ColumnSpec, date_format: str | None = None) -> Callable[[Any], Any]:
        """A function that turns a source value into this column's type, or None."""
        convert = _CONVERTERS[column.type]

        def run(value: Any) -> Any:
            if value is None:
                return None
            try:
                return convert(value, column, date_format)
            except _Null as null:
                if null.invalid_date and self.invalid_dates == "fail":
                    raise ConnectorError(self._stop_message(column, null.reason, value)) from None
                self.null(column, null.reason, value)
                return None

        return run

    def null(self, column: ColumnSpec, reason: str, raw: Any) -> None:
        """Count a value that was stored as NULL."""
        issues = self._issues.get(column.name)
        if issues is None:
            issues = self._issues[column.name] = _Issues(
                column.name, column.type.value, self._source(column)
            )
        issues.count += 1
        issues.reasons[reason] += 1
        sample = _sample(raw)
        if len(issues.samples) < MAX_SAMPLES and sample not in issues.samples:
            issues.samples.append(sample)

    def _of(self) -> str:
        return f" of {self.table!r}" if self.table else ""

    def _source(self, column: ColumnSpec) -> str:
        return self.source_types.get(column.name) or column.source_type or column.type.value

    def _stop_message(self, column: ColumnSpec, reason: str, raw: Any) -> str:
        return (
            f"Column {column.name!r}{self._of()} ({self._source(column)}) holds an invalid date "
            f"({reason}), for example {_sample(raw)!r}. This connection is set to stop on "
            "invalid dates: edit it and choose to store them as empty (NULL) or as text."
        )

    def array(self, values: list[Any], column: ColumnSpec) -> pa.Array:
        """An Arrow array of converted values. Should a value still not fit, it is
        stored as NULL and counted, never failing the read."""
        kind = arrow_type(column)
        try:
            return pa.array(values, type=kind)
        except _ARROW_ERRORS:
            pass
        kept = []
        for value in values:
            try:
                pa.array([value], type=kind)
                kept.append(value)
            except _ARROW_ERRORS:
                self.null(column, f"cannot be stored as {column.type.value}", value)
                kept.append(None)
        return pa.array(kept, type=kind)

    # ---- after the connector: overrides and suggestions --------------------------------

    def shape(
        self, batches: Iterator[pa.RecordBatch], overrides: dict[str, dict[str, Any]] | None
    ) -> Iterator[pa.RecordBatch]:
        """Apply a dataset's type overrides, and look at text columns for suggestions."""
        self.overrides = {
            name: chosen
            for name, chosen in (overrides or {}).items()
            if isinstance(chosen, dict) and chosen.get("type") in OVERRIDE_TYPES
        }
        for batch in batches:
            if self.overrides:
                batch = self._override(batch)
            self._observe(batch)
            yield batch

    def _override(self, batch: pa.RecordBatch) -> pa.RecordBatch:
        arrays, fields = list(batch.columns), list(batch.schema)
        for index, name in enumerate(batch.schema.names):
            chosen = self.overrides.get(name)
            if chosen is None:
                continue
            target = ColumnSpec(name=name, type=OVERRIDE_TYPES[chosen["type"]])
            self.source_types.setdefault(name, str(batch.schema.field(index).type))
            date_format = chosen.get("format") or None
            if target.type not in (ColumnType.DATE, ColumnType.TIMESTAMP):
                date_format = None
            arrays[index] = self.values(batch.column(index).to_pylist(), target, date_format)
            fields[index] = pa.field(name, arrow_type(target))
        return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))

    def _observe(self, batch: pa.RecordBatch) -> None:
        for index, name in enumerate(batch.schema.names):
            if name in self.overrides or not pa.types.is_string(batch.schema.field(index).type):
                continue
            seen = self._texts.setdefault(name, [])
            if len(seen) >= SUGGEST_SAMPLE:
                continue
            for value in batch.column(index).to_pylist():
                if value is not None and value.strip():
                    seen.append(value.strip())
                    if len(seen) >= SUGGEST_SAMPLE:
                        break

    def suggestions(self) -> list[dict[str, Any]]:
        found = []
        for name, values in self._texts.items():
            if name in self.text_dates:
                continue  # read as text by the connection's choice
            suggestion = suggest(values)
            if suggestion is not None:
                found.append({"column": name, **suggestion})
        return found

    # ---- the report ----------------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """What this read changed, for the snapshot, the job and the dataset page."""
        emptied = [
            {
                "column": issues.column,
                "type": issues.type,
                "sourceType": issues.source_type,
                "count": issues.count,
                "reasons": [{"reason": r, "count": n} for r, n in issues.reasons.most_common()],
                "samples": issues.samples,
            }
            for issues in self._issues.values()
        ]
        return {
            "invalidDates": self.invalid_dates,
            "emptied": emptied,
            "emptiedValues": sum(e["count"] for e in emptied),
            "textDates": list(self.text_dates),
            "overrides": self.overrides,
            "suggestions": self.suggestions(),
            # The source's own type for each column (varchar(20), datetime), for the page.
            "sourceTypes": dict(self.source_types),
        }


# ---- converters: (value, column, date format) -> value, or raise _Null ----------------------


def _to_string(value: Any, column: ColumnSpec, _: str | None) -> str:
    return as_text(value)


def _to_json(value: Any, column: ColumnSpec, _: str | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode("utf-8", errors="replace")
    return json.dumps(value, default=str)


def _number_text(value: Any) -> str:
    text = as_text(value).strip()
    if _GROUPED.fullmatch(text):
        text = text.replace(",", "")
    return text


def _to_integer(value: Any, column: ColumnSpec, _: str | None) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _Null("not a number")
        if not value.is_integer():
            raise _Null("not a whole number")
        result = int(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise _Null("not a number")
        if value != value.to_integral_value():
            raise _Null("not a whole number")
        result = int(value)
    elif isinstance(value, bytes | bytearray):
        result = int.from_bytes(value, "big")  # MySQL BIT(n)
    else:
        text = _number_text(value)
        if not text:
            raise _Null("empty text")
        if _INTEGER.fullmatch(text):
            result = int(text)
        elif _NUMBER.fullmatch(text):
            number = Decimal(text)
            if number != number.to_integral_value():
                raise _Null("not a whole number")
            result = int(number)
        else:
            raise _Null("not a number")
    if not INT_MIN <= result <= INT_MAX:
        raise _Null("too large for a 64-bit integer")
    return result


def _to_float(value: Any, column: ColumnSpec, _: str | None) -> float:
    if isinstance(value, bool | int | float | Decimal):
        return float(value)
    if isinstance(value, bytes | bytearray):
        return float(int.from_bytes(value, "big"))
    text = _number_text(value)
    if not text or not _NUMBER.fullmatch(text):
        raise _Null("not a number")
    return float(text)


def _to_decimal(value: Any, column: ColumnSpec, date_format: str | None) -> Any:
    if column.scale is None:
        return _to_float(value, column, date_format)
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, bool | int):
            number = Decimal(int(value))
        elif isinstance(value, float):
            number = Decimal(repr(value))
        elif isinstance(value, bytes | bytearray):
            number = Decimal(int.from_bytes(value, "big"))  # MySQL BIT(64)
        else:
            text = _number_text(value)
            if not text or not _NUMBER.fullmatch(text):
                raise _Null("not a number")
            number = Decimal(text)
    except InvalidOperation:
        raise _Null("not a number") from None
    if not number.is_finite():
        raise _Null("not a number")
    with localcontext() as context:
        context.prec = 100
        context.rounding = ROUND_HALF_UP  # as databases round, not to even
        exact = number.quantize(Decimal(1).scaleb(-column.scale))
    if len(exact.as_tuple().digits) > DECIMAL_DIGITS:
        raise _Null("too large for a decimal")
    return exact


def _to_boolean(value: Any, column: ColumnSpec, _: str | None) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | Decimal):
        return value != 0
    if isinstance(value, float):
        if value != value:
            raise _Null("not true or false")
        return value != 0
    if isinstance(value, bytes | bytearray):
        return any(value)  # MySQL BIT(1)
    text = as_text(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise _Null("not true or false")


def _parse(text: str, date_format: str | None) -> datetime:
    """A date or date-time from text, in the given format or else ISO 8601."""
    reason = invalid_date(text)
    if reason:
        raise _Null(reason, invalid_date=True)
    try:
        if date_format:
            return datetime.strptime(text, date_format)
        return datetime.fromisoformat(text)
    except ValueError:
        raise _Null(f"not a date like {date_format}" if date_format else "not a date") from None


def _to_date(value: Any, column: ColumnSpec, date_format: str | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float | Decimal | bool):
        raise _Null("not a date")
    text = as_text(value).strip()
    if not text:
        raise _Null("empty text")
    return _parse(text, date_format).date()


def _to_timestamp(value: Any, column: ColumnSpec, date_format: str | None) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, int | float | Decimal | bool):
        raise _Null("not a date and time")
    else:
        text = as_text(value).strip()
        if not text:
            raise _Null("empty text")
        moment = _parse(text, date_format)
    if column.tz_aware:  # an instant: keep it, in UTC
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    # Wall-clock time: drop any offset rather than shifting the clock.
    return moment.replace(tzinfo=None)


_CONVERTERS: dict[ColumnType, Callable[[Any, ColumnSpec, str | None], Any]] = {
    ColumnType.STRING: _to_string,
    ColumnType.JSON: _to_json,
    ColumnType.INTEGER: _to_integer,
    ColumnType.FLOAT: _to_float,
    ColumnType.DECIMAL: _to_decimal,
    ColumnType.BOOLEAN: _to_boolean,
    ColumnType.DATE: _to_date,
    ColumnType.TIMESTAMP: _to_timestamp,
}


# ---- suggestions for text columns -------------------------------------------------------------


def _date_format(values: list[str], formats: Sequence[str | None]) -> str | None:
    """The first format in which every value is a date or an invalid date such as
    0000-00-00: "iso" for ISO 8601, else the format itself; None when none fits."""
    for date_format in formats:
        ok = True
        for value in values:
            try:
                _parse(value, date_format)
            except _Null as null:
                if not null.invalid_date:
                    ok = False
                    break
        if ok:
            return date_format or "iso"
    return None


def suggest(values: list[str]) -> dict[str, Any] | None:
    """A type for text values that are consistently something else, or None."""
    present = [v for v in values if v and v.lower() not in EMPTY_WORDS]
    if len(present) < SUGGEST_MINIMUM:
        return None
    blanks = len(values) - len(present)
    lowered = {v.lower() for v in present}
    result: dict[str, Any] = {"format": None, "checked": len(values)}
    if blanks:
        result["emptyWords"] = blanks  # "n/a" and the like, which would become NULL
    if lowered <= TRUE_WORDS | FALSE_WORDS and not lowered <= {"0", "1"}:
        return {"type": "boolean", **result}
    numbers = [_number_text(v) for v in present]
    if all(_INTEGER.fullmatch(n) for n in numbers):
        # Codes with leading zeros (postcodes, phone numbers) are text on purpose.
        if any(len(n.lstrip("+-")) > 1 and n.lstrip("+-").startswith("0") for n in numbers):
            return None
        if not all(INT_MIN <= int(n) <= INT_MAX for n in numbers):
            return None
        return {"type": "integer", **result}
    if all(_NUMBER.fullmatch(n) for n in numbers):
        return {"type": "decimal", **result}
    invalid = sum(1 for v in present if invalid_date(v))
    if len(present) - invalid < SUGGEST_MINIMUM:
        return None
    kinds: list[tuple[str, tuple[str | None, ...]]] = [("date", (None, *DATE_FORMATS))]
    if any(":" in v for v in present):
        kinds.insert(0, ("datetime", (None, *DATETIME_FORMATS)))
    for kind, formats in kinds:
        found = _date_format(present, formats)
        if found is None:
            continue
        suggestion: dict[str, Any] = {
            **result,
            "type": kind,
            "format": None if found == "iso" else found,
        }
        if invalid:
            suggestion["invalidDates"] = invalid
        if found.startswith("%d/%m/%Y"):
            swapped = found.replace("%d/%m", "%m/%d", 1)
            if _date_format(present, (swapped,)):
                suggestion["note"] = (
                    "Every value reads as day first and as month first; day first is suggested."
                )
        return suggestion
    return None


def check_format(date_format: str) -> str:
    """A date format that can be read and written back; ValueError says why not."""
    text = date_format.strip()
    if "%" not in text or len(text) > 60:
        raise ValueError("Write the format with % codes, for example %d/%m/%Y for 31/12/2026.")
    sample = datetime(2026, 12, 31, 13, 45, 59)
    try:
        written = sample.strftime(text)
        datetime.strptime(written, text)
    except ValueError as exc:
        raise ValueError(f"This format cannot be used: {exc}.") from None
    if "%Y" not in text and "%y" not in text:
        raise ValueError("The format needs a year (%Y, or %y for two digits).")
    return text
