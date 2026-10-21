"""
Pure (Home Assistant free) conversion between the HA 'schedule' helper
representation of a weekly schedule and the Meross thermostats native
Schedule/ScheduleB payload.

Helper format (what schedule.get_schedule returns under the entity_id key,
or what our set_schedule service accepts):

    {
        "monday": [
            {"from": "06:30", "to": "08:00", "data": {"temperature": 21.5}},
            ...
        ],
        ...
    }

'from'/'to' can be either datetime.time objects (time.max meaning 24:00) or
"HH:MM[:SS[.ffffff]]" strings ("24:00" / "24:00:00" meaning end of day).

Native format (the per-channel/per-subdevice dict carried in ns_value):

    {"mon": [[duration_minutes, temperature_scaled], ...], ..., "sun": [...]}

Each native day sums to exactly 1440 minutes.

Errors in user supplied data are raised as ScheduleValidationError carrying a
stable `code` (usable as a translation key) and string `placeholders`.
Programming errors (inconsistent device limits) are raised as plain ValueError.
"""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
from datetime import time
import math
import re
import typing

if typing.TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import Any, Literal

    HelperBlockType = Mapping[str, Any]
    HelperScheduleType = Mapping[str, Iterable[HelperBlockType] | None]
    NativeEntryType = list[int]
    NativeDayType = list[NativeEntryType]
    NativeScheduleType = dict[str, NativeDayType]
    NativeMappingType = Mapping[str, Any]


MTS_SCHEDULE_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HA_SCHEDULE_WEEKDAY = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
MINUTES_PER_DAY = 1440

KEY_FROM = "from"
KEY_TO = "to"
KEY_DATA = "data"
DEFAULT_TEMPERATURE_KEY = "temperature"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$")


class ScheduleValidationError(ValueError):
    """
    Raised when a schedule (helper or native) cannot be converted.

    - code: stable identifier of the error kind (usable as a translation key)
    - placeholders: str -> str mapping with the details of the error
      (weekday, range, index, value, ...)
    - str(e): an English, human readable message
    """

    __slots__ = ("code", "placeholders")

    code: str
    placeholders: dict[str, str]

    def __init__(self, code: str, message: str, /, **placeholders: str):
        super().__init__(message)
        self.code = code
        self.placeholders = placeholders


@dataclasses.dataclass(slots=True)
class CompileResult:
    """
    Result of compile_schedule:
    - native: all 7 mts weekdays ("mon".."sun"), each summing to 1440 minutes
    - warnings: human readable notes about any adjustment (rounding of times,
      snapping/clamping of temperatures, dropped blocks)
    """

    native: NativeScheduleType
    warnings: list[str]


def _fmt(minutes: float) -> str:
    """Format minutes since midnight as "HH:MM" ("HH:MM:SS" if not whole).
    1440 is rendered as "24:00"."""
    total_seconds = int(round(minutes * 60))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}" if not s else f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_range(begin: float, end: float) -> str:
    return f"{_fmt(begin)}-{_fmt(end)}"


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _round_to_unit(minutes: float, unit_time: int) -> int:
    """Round minutes to the nearest multiple of unit_time (ties go up),
    saturating at end of day."""
    return min(MINUTES_PER_DAY, _round_half_up(minutes / unit_time) * unit_time)


def normalize_time(value: Any) -> float:
    """
    Convert a schedule boundary to (possibly fractional) minutes since midnight.

    Accepts datetime.time (time.max meaning 24:00) or "HH:MM[:SS[.ffffff]]"
    strings, where "24:00" / "24:00:00" (and "23:59:59.999999") mean end of day
    and are returned as 1440.0.
    Raises ScheduleValidationError("time_invalid") for anything else.
    """
    if isinstance(value, time):
        h, m, s, us = value.hour, value.minute, value.second, value.microsecond
    elif isinstance(value, str) and (match := _TIME_RE.match(value.strip())):
        hour, minute, second, fraction = match.groups()
        h, m, s = int(hour), int(minute), int(second or 0)
        us = int((fraction or "0").ljust(6, "0"))
        if h == 24 and m == 0 and s == 0 and us == 0:
            return float(MINUTES_PER_DAY)
        if h > 23 or m > 59 or s > 59:
            raise ScheduleValidationError(
                "time_invalid", f"Invalid time value: {value!r}", value=repr(value)
            )
    else:
        raise ScheduleValidationError(
            "time_invalid", f"Invalid time value: {value!r}", value=repr(value)
        )
    if (h, m, s, us) == (23, 59, 59, 999999):
        # time.max is how HA represents 24:00
        return float(MINUTES_PER_DAY)
    return h * 60 + m + s / 60 + us / 60_000_000


def normalize_temperature(
    value: Any, *, scale: int, min_temp: float, max_temp: float, temp_step: float
) -> tuple[int, str | None]:
    """
    Convert a temperature (number or numeric string) to the device scaled int.

    The value is scaled (round half up), snapped to temp_step (in the scaled
    domain, only when the scaled step is > 1) and clamped to [min_temp, max_temp].
    Returns (scaled, warning) where warning is None when no adjustment was needed.
    Raises ScheduleValidationError("temperature_invalid") for booleans,
    unparsable or non finite values.
    """
    try:
        if isinstance(value, bool):
            raise TypeError
        temperature = float(value)
    except (TypeError, ValueError):
        raise ScheduleValidationError(
            "temperature_invalid", f"Invalid temperature: {value!r}", value=repr(value)
        ) from None
    if not math.isfinite(temperature):
        raise ScheduleValidationError(
            "temperature_invalid", f"Invalid temperature: {value!r}", value=repr(value)
        )
    scaled = _round_half_up(temperature * scale)
    result = scaled
    step_scaled = _round_half_up(temp_step * scale) if temp_step else 0
    if step_scaled > 1:
        result = _round_half_up(result / step_scaled) * step_scaled
    min_scaled = _round_half_up(min_temp * scale)
    max_scaled = _round_half_up(max_temp * scale)
    result = max(min_scaled, min(max_scaled, result))
    warning = (
        None
        if result == scaled
        else f"temperature {temperature:g} adjusted to {result / scale:g}"
    )
    return result, warning


def compile_schedule(
    helper: HelperScheduleType,
    *,
    scale: int,
    min_temp: float,
    max_temp: float,
    temp_step: float = 0.5,
    unit_time: int = 15,
    entry_count_max: int = 0,
    entry_count_min: int = 0,
    gap_temperature: float | None = None,
    block_temperature_default: float | None = None,
    temperature_key: str = DEFAULT_TEMPERATURE_KEY,
) -> CompileResult:
    """
    Compile an HA schedule helper representation into the native device payload.

    - helper: {"monday": [{"from", "to", "data": {temperature_key: T}}], ...}
      weekday keys are matched case and whitespace insensitively; missing
      (or None) days are treated as empty.
    - scale: device temperature scale (10 for mts1xx/2xx, 100 for mts9xx).
    - min_temp/max_temp/temp_step: device setpoint limits (in degrees).
    - unit_time: device schedule granularity in minutes (must divide 1440).
      Every block boundary is rounded to the nearest unit (ties go up) so that
      touching blocks keep touching; blocks collapsing to zero length are dropped.
    - entry_count_max: max number of entries per day (0 = unlimited).
    - entry_count_min: days with fewer entries are padded with zero-duration
      entries carrying the first entry temperature (same as the calendar entity).
    - gap_temperature: when None (strict mode) any uncovered time raises
      "day_gap"/"day_empty"; otherwise gaps are filled with this temperature.
    - block_temperature_default: used for blocks without temperature_key data;
      when None such blocks raise "block_missing_temperature".

    Raises ValueError for inconsistent limits (programming errors) and
    ScheduleValidationError for invalid helper data. The device is never
    expected to be written when an exception is raised.
    """
    if unit_time < 1 or MINUTES_PER_DAY % unit_time:
        raise ValueError(
            f"unit_time must be a divisor of {MINUTES_PER_DAY}: {unit_time}"
        )
    if entry_count_max and entry_count_min > entry_count_max:
        raise ValueError("entry_count_min cannot exceed entry_count_max")
    if min_temp > max_temp:
        raise ValueError("min_temp cannot exceed max_temp")

    warnings: list[str] = []
    temp_kwargs = {
        "scale": scale,
        "min_temp": min_temp,
        "max_temp": max_temp,
        "temp_step": temp_step,
    }
    gap_scaled = default_scaled = None
    if gap_temperature is not None:
        gap_scaled, warning = normalize_temperature(gap_temperature, **temp_kwargs)
        if warning:
            warnings.append(f"gap {warning}")
    if block_temperature_default is not None:
        default_scaled, warning = normalize_temperature(
            block_temperature_default, **temp_kwargs
        )
        if warning:
            warnings.append(f"default {warning}")

    days: dict[str, Any] = {}
    for key, value in helper.items():
        ha_day = str(key).strip().lower()
        if ha_day not in HA_SCHEDULE_WEEKDAY:
            raise ScheduleValidationError(
                "unknown_weekday", f"Unknown weekday '{key}'", weekday=str(key)
            )
        days[ha_day] = value

    native = {
        mts_day: _compile_day(
            ha_day,
            days.get(ha_day) or (),
            unit_time=unit_time,
            entry_count_max=entry_count_max,
            entry_count_min=entry_count_min,
            gap_scaled=gap_scaled,
            default_scaled=default_scaled,
            temperature_key=temperature_key,
            temp_kwargs=temp_kwargs,
            warnings=warnings,
        )
        for ha_day, mts_day in zip(HA_SCHEDULE_WEEKDAY, MTS_SCHEDULE_WEEKDAY)
    }
    return CompileResult(native, warnings)


def _compile_day(
    day: str,
    blocks: Iterable[Any],
    *,
    unit_time: int,
    entry_count_max: int,
    entry_count_min: int,
    gap_scaled: int | None,
    default_scaled: int | None,
    temperature_key: str,
    temp_kwargs: dict[str, Any],
    warnings: list[str],
) -> NativeDayType:
    """Compile the blocks of a single (HA named) weekday into native entries."""
    if isinstance(blocks, (str, bytes, Mapping)):
        blocks = (blocks,)  # reported as an invalid block below
    try:
        blocks = list(blocks)
    except TypeError:
        blocks = [blocks]  # reported as an invalid block below

    # 1. parse and validate each block (raw, un-rounded minutes)
    parsed: list[tuple[float, float, int]] = []
    for index, block in enumerate(blocks):
        if (
            not isinstance(block, Mapping)
            or KEY_FROM not in block
            or KEY_TO not in block
        ):
            raise ScheduleValidationError(
                "block_invalid",
                f"{day}: block #{index} must contain '{KEY_FROM}' and '{KEY_TO}'",
                weekday=day,
                index=str(index),
            )
        try:
            begin = normalize_time(block[KEY_FROM])
            end = normalize_time(block[KEY_TO])
        except ScheduleValidationError as e:
            raise ScheduleValidationError(
                e.code,
                f"{day}: block #{index}: {e}",
                **{**e.placeholders, "weekday": day, "index": str(index)},
            ) from None
        rng = _fmt_range(begin, end)
        if begin >= end:
            raise ScheduleValidationError(
                "block_invalid_range",
                f"{day}: block {rng} must end after it starts "
                "(blocks cannot cross midnight)",
                weekday=day,
                range=rng,
            )
        data = block.get(KEY_DATA)
        temperature = data.get(temperature_key) if isinstance(data, Mapping) else None
        if temperature is None:
            if default_scaled is None:
                raise ScheduleValidationError(
                    "block_missing_temperature",
                    f"{day}: block {rng} has no '{temperature_key}'",
                    weekday=day,
                    range=rng,
                    key=temperature_key,
                )
            scaled = default_scaled
        else:
            try:
                scaled, warning = normalize_temperature(temperature, **temp_kwargs)
            except ScheduleValidationError as e:
                raise ScheduleValidationError(
                    e.code,
                    f"{day}: block {rng}: {e}",
                    **{**e.placeholders, "weekday": day, "range": rng},
                ) from None
            if warning:
                warnings.append(f"{day}: block {rng}: {warning}")
        parsed.append((begin, end, scaled))

    # 2. sort by start and reject raw overlaps
    # (rounding is monotonic so it cannot introduce new overlaps)
    parsed.sort(key=lambda b: (b[0], b[1]))
    for prev, cur in zip(parsed, parsed[1:]):
        if cur[0] < prev[1]:
            range_a = _fmt_range(prev[0], prev[1])
            range_b = _fmt_range(cur[0], cur[1])
            raise ScheduleValidationError(
                "block_overlap",
                f"{day}: blocks {range_a} and {range_b} overlap",
                weekday=day,
                range_a=range_a,
                range_b=range_b,
            )

    # 3. align boundaries to unit_time (nearest, half-up) and drop collapsed blocks
    segments: list[tuple[int, int, int]] = []
    for begin, end, scaled in parsed:
        r_begin = _round_to_unit(begin, unit_time)
        r_end = _round_to_unit(end, unit_time)
        if r_begin != begin or r_end != end:
            warnings.append(
                f"{day}: block {_fmt_range(begin, end)} aligned to "
                f"{_fmt_range(r_begin, r_end)} ({unit_time} minutes granularity)"
            )
        if r_begin >= r_end:
            warnings.append(
                f"{day}: block {_fmt_range(begin, end)} shorter than "
                f"{unit_time} minutes dropped"
            )
            continue
        segments.append((r_begin, r_end, scaled))

    # 4. walk the day: fill or reject gaps, build entries merging equal temperatures
    entries: NativeDayType = []

    def _append(duration: int, temperature: int):
        if entries and entries[-1][1] == temperature:
            entries[-1][0] += duration
        else:
            entries.append([duration, temperature])

    def _gap(begin: int, end: int):
        if gap_scaled is None:
            if not blocks:
                raise ScheduleValidationError(
                    "day_empty", f"{day}: no schedule blocks defined", weekday=day
                )
            rng = _fmt_range(begin, end)
            raise ScheduleValidationError(
                "day_gap",
                f"{day}: {rng} is not covered by any block",
                weekday=day,
                range=rng,
            )
        _append(end - begin, gap_scaled)

    cursor = 0
    for r_begin, r_end, scaled in segments:
        if r_begin < cursor:  # defensive: cannot happen after step 2
            raise ScheduleValidationError(
                "block_overlap",
                f"{day}: overlapping blocks after alignment",
                weekday=day,
                range_a=_fmt(cursor),
                range_b=_fmt(r_begin),
            )
        if r_begin > cursor:
            _gap(cursor, r_begin)
        _append(r_end - r_begin, scaled)
        cursor = r_end
    if cursor < MINUTES_PER_DAY:
        _gap(cursor, MINUTES_PER_DAY)
    assert sum(entry[0] for entry in entries) == MINUTES_PER_DAY

    # 5. device entry count constraints
    if entry_count_max and len(entries) > entry_count_max:
        raise ScheduleValidationError(
            "too_many_entries",
            f"{day}: {len(entries)} schedule entries exceed "
            f"the device limit of {entry_count_max}",
            weekday=day,
            count=str(len(entries)),
            max=str(entry_count_max),
        )
    # same padding convention as calendar.MtsSchedule._async_request_schedule
    # (fresh lists so that entries are never aliased)
    for _ in range(entry_count_min - len(entries)):
        entries.append([0, entries[0][1]])
    return entries


def _minutes_to_output(minutes: int, time_format: str) -> str | time:
    if time_format == "time":
        if minutes >= MINUTES_PER_DAY:
            return time.max
        return time(minutes // 60, minutes % 60)
    return _fmt(minutes)


def decompile_schedule(
    native: NativeMappingType,
    *,
    scale: int,
    temperature_key: str = DEFAULT_TEMPERATURE_KEY,
    time_format: Literal["str", "time"] = "str",
) -> dict[str, list[dict[str, Any]]]:
    """
    Convert a native device payload into the HA schedule helper representation.

    Only weekdays present in native are emitted (keyed by HA weekday name);
    any other key (channel, section, id, ...) is ignored. Zero-duration entries
    are skipped and consecutive entries with the same temperature are merged.
    Times are emitted as "HH:MM" strings ("24:00" for end of day) or, with
    time_format="time", as datetime.time objects (time.max for end of day).

    Raises ScheduleValidationError("unsupported_entry_width") for entries not
    shaped like [minutes, temperature] (e.g. mts300 3-value entries) and
    ScheduleValidationError("native_invalid") for malformed payloads.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for ha_day, mts_day in zip(HA_SCHEDULE_WEEKDAY, MTS_SCHEDULE_WEEKDAY):
        if mts_day not in native:
            continue
        day_entries = native[mts_day]
        if not isinstance(day_entries, (list, tuple)):
            raise ScheduleValidationError(
                "native_invalid",
                f"{mts_day}: entries must be a list",
                weekday=mts_day,
            )
        segments: list[list[int]] = []  # [begin, end, scaled]
        cursor = 0
        for index, entry in enumerate(day_entries):
            try:
                width = len(entry)
                duration = int(entry[0])
                scaled = int(entry[1])
            except (TypeError, ValueError, IndexError):
                raise ScheduleValidationError(
                    "native_invalid",
                    f"{mts_day}: entry #{index} is not [minutes, temperature]",
                    weekday=mts_day,
                    index=str(index),
                ) from None
            if width != 2:
                raise ScheduleValidationError(
                    "unsupported_entry_width",
                    f"{mts_day}: entry #{index} has {width} values "
                    "(only [minutes, temperature] is supported)",
                    weekday=mts_day,
                    width=str(width),
                )
            if duration < 0 or cursor + duration > MINUTES_PER_DAY:
                raise ScheduleValidationError(
                    "native_invalid",
                    f"{mts_day}: entry #{index} has an invalid duration {duration}",
                    weekday=mts_day,
                    index=str(index),
                )
            if duration == 0:
                continue
            end = cursor + duration
            if segments and segments[-1][2] == scaled:
                segments[-1][1] = end
            else:
                segments.append([cursor, end, scaled])
            cursor = end
        if segments and cursor != MINUTES_PER_DAY:
            raise ScheduleValidationError(
                "native_invalid",
                f"{mts_day}: entries cover {cursor} minutes "
                f"instead of {MINUTES_PER_DAY}",
                weekday=mts_day,
            )
        result[ha_day] = [
            {
                KEY_FROM: _minutes_to_output(begin, time_format),
                KEY_TO: _minutes_to_output(end, time_format),
                KEY_DATA: {temperature_key: scaled / scale},
            }
            for begin, end, scaled in segments
        ]
    return result


def normalize_native_day(entries: Iterable[Sequence[int]]) -> NativeDayType:
    """
    Return a canonical copy of a native day: zero (or negative) duration entries
    are dropped and consecutive entries with equal payload (entry[1:]) are merged.
    Width agnostic (works on [minutes, heat, cold] entries too).
    """
    result: NativeDayType = []
    for entry in entries:
        duration = entry[0]
        if duration <= 0:
            continue
        if result and result[-1][1:] == list(entry[1:]):
            result[-1][0] += duration
        else:
            result.append(list(entry))
    return result


def native_equal(a: NativeMappingType, b: NativeMappingType) -> bool:
    """
    Compare two native payloads on the 7 mts weekdays only (other keys like
    channel/section/id are ignored), after normalize_native_day (so padding and
    split entries with equal temperatures do not matter).
    A weekday present on one side only makes them different.
    """
    for day in MTS_SCHEDULE_WEEKDAY:
        a_day, b_day = a.get(day), b.get(day)
        if (a_day is None) != (b_day is None):
            return False
        if a_day is not None and normalize_native_day(a_day) != normalize_native_day(
            b_day  # type: ignore[arg-type]
        ):
            return False
    return True
