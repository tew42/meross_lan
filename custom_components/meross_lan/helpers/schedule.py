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

The block data keys are configurable ('keys'): 'temperature' by default,
'heat' and 'cool' for the mts300 ({"heat": 20, "cool": 26}). Helper values are
in °C or, with 'fahrenheit', in °F (device values are always °C).
Time not covered by any block is "off" (see compile_schedule).

'from'/'to' can be either datetime.time objects (time.max meaning 24:00) or
"HH:MM[:SS[.ffffff]]" strings ("24:00" / "24:00:00" meaning end of day).

Native format (the per-channel/per-subdevice dict carried in ns_value):

    {"mon": [[duration_minutes, temperature_scaled], ...], ..., "sun": [...]}

Entries carry one scaled value per key ([minutes, heat, cool] on the mts300).
Each native day sums to exactly 1440 minutes.

Errors in user supplied data are raised as ScheduleValidationError carrying a
stable `code`. Programming errors (inconsistent device limits) are raised as
plain ValueError.
"""

from collections.abc import Mapping
import dataclasses
from datetime import time
import math
import re
import typing

if typing.TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence
    from typing import Any

    HelperScheduleType = Mapping[str, Sequence[Mapping[str, Any]] | None]
    NativeEntryType = list[int]
    NativeDayType = list[NativeEntryType]
    NativeScheduleType = dict[str, NativeDayType]
    NativeMappingType = Mapping[str, Any]
    # aligned block: (begin, end, raw "HH:MM-HH:MM" range, scaled values)
    BlockType = tuple[int, int, str, list[Any]]


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
KEY_TEMPERATURE = "temperature"
KEY_HEAT = "heat"
KEY_COOL = "cool"

_TIME_RE = re.compile(r"^([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,6}))?)?$")


class ScheduleValidationError(ValueError):
    """
    Raised when a schedule (helper or native) cannot be converted.
    `code` is a stable identifier of the error kind while str(e) is an
    English, human readable message.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(slots=True)
class CompileResult:
    """
    Result of compile_schedule:
    - native: all 7 mts weekdays ("mon".."sun"), each summing to 1440 minutes
    - warnings: human readable notes about any adjustment (rounding of times,
      rounding/clamping of temperatures, dropped blocks, carried values)
    """

    native: "NativeScheduleType"
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
    """Round minutes to the nearest multiple of unit_time (ties go up).
    Since minutes <= 1440 and unit_time divides 1440 the result never
    exceeds 1440."""
    return _round_half_up(minutes / unit_time) * unit_time


def _to_grid(celsius: float, scale: int, device_fahrenheit: bool) -> int:
    """celsius on the device grid, scaled: the nearest 0.5 °C or, on a °F
    device, the nearest whole °F converted to °C and truncated to the device
    scale (as the devices store it: 75 °F is 2388 at scale 100)."""
    if device_fahrenheit:
        fahrenheit = _round_half_up(celsius * 1.8 + 32)
        return math.trunc((fahrenheit - 32) * 5 * scale / 9)
    return _round_half_up(celsius * 2) * scale // 2


def _to_helper_unit(
    scaled: int, scale: int, fahrenheit: bool, device_fahrenheit: bool = False
) -> float:
    """A device (scaled °C) value in the helper unit (°C or °F): the whole °F a
    °F device grid point represents or else the shortest decimal (up to 2
    digits) converting back to the same scaled value."""
    value = scaled / scale * 1.8 + 32 if fahrenheit else scaled / scale
    if fahrenheit and device_fahrenheit:
        whole = _round_half_up(value)
        if _to_grid((whole - 32) / 1.8, scale, True) == scaled:
            return float(whole)
    for digits in (0, 1):
        rounded = round(value, digits)
        celsius = (rounded - 32) / 1.8 if fahrenheit else rounded
        if _round_half_up(celsius * scale) == scaled:
            return rounded
    return round(value, 2)


def normalize_time(value: "Any") -> float:
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
        if (h, m, s, us) == (24, 0, 0, 0):
            return float(MINUTES_PER_DAY)
        if h > 23 or m > 59 or s > 59:
            raise ScheduleValidationError(
                "time_invalid", f"Invalid time value: {value!r}"
            )
    else:
        raise ScheduleValidationError("time_invalid", f"Invalid time value: {value!r}")
    if (h, m, s, us) == (23, 59, 59, 999999):
        # time.max is how HA represents 24:00
        return float(MINUTES_PER_DAY)
    return h * 60 + m + s / 60 + us / 60_000_000


def normalize_temperature(
    value: "Any",
    *,
    scale: int,
    min_temp: float,
    max_temp: float,
    fahrenheit: bool = False,
    device_fahrenheit: bool = False,
    keep: "Collection[int]" = (),
) -> tuple[int, str | None]:
    """
    Convert a temperature (number or numeric string, in °F with fahrenheit) to
    the device scaled (°C) int.

    The value is converted to °C and rounded to the device grid (see _to_grid)
    unless its exact scaled value is in keep (values the device holds are
    storable as they are), then clamped to [min_temp, max_temp] (°C).
    Returns (scaled, warning) where warning is None when the result, converted
    back as decompile_schedule does, equals the input value.
    Raises ScheduleValidationError("temperature_invalid") for booleans,
    unparsable, non finite or overflowing values.
    """
    try:
        if isinstance(value, bool):
            raise TypeError
        temperature = float(value)
        celsius = (temperature - 32) / 1.8 if fahrenheit else temperature
        # rounding raises ValueError for nan, OverflowError for inf/huge values
        result = _round_half_up(celsius * scale)
        if result not in keep:
            result = _to_grid(celsius, scale, device_fahrenheit)
    except (TypeError, ValueError, OverflowError):
        raise ScheduleValidationError(
            "temperature_invalid", f"Invalid temperature: {value!r}"
        ) from None
    result = max(
        _round_half_up(min_temp * scale),
        min(_round_half_up(max_temp * scale), result),
    )
    adjusted = _to_helper_unit(result, scale, fahrenheit, device_fahrenheit)
    if adjusted == temperature:
        return result, None
    return result, f"temperature {temperature:g} adjusted to {adjusted:g}"


def compile_schedule(
    helper: "HelperScheduleType",
    *,
    scale: int,
    min_temp: float,
    max_temp: float,
    unit_time: int = 15,
    entry_count_max: int = 0,
    entry_count_min: int = 0,
    keys: "Sequence[str]" = (KEY_TEMPERATURE,),
    fahrenheit: bool = False,
    device_fahrenheit: bool = False,
    off_values: "Sequence[int] | None" = None,
    keep: "Collection[int]" = (),
) -> CompileResult:
    """
    Compile an HA schedule helper representation into the native device payload.

    Every block becomes its own entry (blocks with equal values are not merged).
    Time not covered by any block (gaps, the time before the first and after the
    last block of a day, missing, None or empty days) is "off": an entry of its
    own carrying off_values.
    - scale: device temperature scale (10 for mts1xx/2xx, 100 for mts9xx/mts300).
    - min_temp/max_temp: device setpoint limits (in °C).
    - fahrenheit: helper values are in °F.
    - device_fahrenheit: the device displays °F: values are rounded to whole °F
      instead of 0.5 °C (see normalize_temperature).
    - off_values: the scaled values of uncovered time, one per key (default:
      max_temp for 'cool', min_temp for any other key).
    - keep: scaled values not rounded to the device grid (see
      normalize_temperature).
    - unit_time: device schedule granularity in minutes (must divide 1440).
      Every block boundary is rounded to the nearest unit (ties go up) so that
      touching blocks keep touching; blocks collapsing to zero length are dropped.
    - entry_count_max: max number of entries per day (off ones included),
      padding excluded (0 = unlimited).
    - entry_count_min: days with fewer entries are padded with zero-duration
      entries repeating the first entry values.
    - keys: with more than one key a block missing a value takes the one of the
      previous (not dropped) block in time (see _carry_forward) and 'heat' must
      be below 'cool'.
    """
    if unit_time < 1 or MINUTES_PER_DAY % unit_time:
        raise ValueError(
            f"unit_time must be a divisor of {MINUTES_PER_DAY}: {unit_time}"
        )
    if entry_count_max and entry_count_min > entry_count_max:
        raise ValueError("entry_count_min cannot exceed entry_count_max")
    if min_temp > max_temp:
        raise ValueError("min_temp cannot exceed max_temp")
    if off_values is None:
        off_values = [
            _round_half_up((max_temp if key == KEY_COOL else min_temp) * scale)
            for key in keys
        ]
    elif len(off_values) != len(keys):
        raise ValueError("off_values must carry one value per key")

    for key in helper:
        if key not in HA_SCHEDULE_WEEKDAY:
            raise ScheduleValidationError("unknown_weekday", f"Unknown weekday '{key}'")

    warnings: list[str] = []
    temp_kwargs = {
        "scale": scale,
        "min_temp": min_temp,
        "max_temp": max_temp,
        "fahrenheit": fahrenheit,
        "device_fahrenheit": device_fahrenheit,
        "keep": keep,
    }
    week = {
        day: _parse_day(
            day,
            helper.get(day) or (),
            keys=keys,
            unit_time=unit_time,
            temp_kwargs=temp_kwargs,
            warnings=warnings,
        )
        for day in HA_SCHEDULE_WEEKDAY
    }
    if not any(week.values()):
        raise ScheduleValidationError("schedule_empty", "the schedule has no blocks")
    if len(keys) > 1:
        # invalid blocks raise: fewer blocks kept means some were dropped
        dropped = sum(len(b or ()) for b in helper.values()) > sum(
            map(len, week.values())
        )
        note = f" (blocks shorter than {unit_time} minutes are dropped)"
        _carry_forward(week, keys, warnings, note if dropped else "")
    if tuple(keys) == (KEY_HEAT, KEY_COOL):
        for day, blocks in week.items():
            for _, _, rng, values in blocks:
                if values[0] >= values[1]:
                    heat, cool = (
                        _to_helper_unit(v, scale, fahrenheit, device_fahrenheit)
                        for v in values
                    )
                    raise ScheduleValidationError(
                        "setpoint_order",
                        f"{day}: block {rng}: {KEY_HEAT} {heat:g} must be below "
                        f"{KEY_COOL} {cool:g}",
                    )
    native: NativeScheduleType = {
        mts_day: _compile_day(
            ha_day,
            week[ha_day],
            off_values,
            entry_count_max=entry_count_max,
            entry_count_min=entry_count_min,
        )
        for ha_day, mts_day in zip(HA_SCHEDULE_WEEKDAY, MTS_SCHEDULE_WEEKDAY)
    }
    return CompileResult(native, warnings)


def _parse_day(
    day: str,
    blocks: "Iterable[Any]",
    *,
    keys: "Sequence[str]",
    unit_time: int,
    temp_kwargs: "dict[str, Any]",
    warnings: list[str],
) -> "list[BlockType]":
    """Parses and validates the blocks of a single (HA named) weekday, then aligns
    them to unit_time. Returns the blocks kept, sorted by time. Missing values
    (multi-key mode only) are None."""
    parsed: list[tuple[float, float, str, list[Any]]] = []
    for index, block in enumerate(blocks):
        if (
            not isinstance(block, Mapping)
            or KEY_FROM not in block
            or KEY_TO not in block
        ):
            raise ScheduleValidationError(
                "block_invalid",
                f"{day}: block #{index} must contain '{KEY_FROM}' and '{KEY_TO}'",
            )
        data = block.get(KEY_DATA)
        if not isinstance(data, Mapping):
            data = {}
        try:
            begin = normalize_time(block[KEY_FROM])
            end = normalize_time(block[KEY_TO])
            rng = _fmt_range(begin, end)
            values: list[int | None] = []
            for key in keys:
                if (value := data.get(key)) is None:
                    if len(keys) == 1:
                        raise ScheduleValidationError(
                            "block_missing_temperature", f"no '{key}' value"
                        )
                    values.append(None)
                    continue
                scaled, warning = normalize_temperature(value, **temp_kwargs)
                if warning:
                    label = f"{key} " if len(keys) > 1 else ""
                    warnings.append(f"{day}: block {rng}: {label}{warning}")
                values.append(scaled)
        except ScheduleValidationError as e:
            raise ScheduleValidationError(
                e.code, f"{day}: block #{index}: {e}"
            ) from None
        if begin >= end:
            raise ScheduleValidationError(
                "block_invalid_range",
                f"{day}: block {rng} must end after it starts "
                "(blocks cannot cross midnight)",
            )
        parsed.append((begin, end, rng, values))

    # rounding is monotonic so it cannot introduce overlaps: check the raw times
    parsed.sort(key=lambda b: (b[0], b[1]))
    for prev, cur in zip(parsed, parsed[1:]):
        if cur[0] < prev[1]:
            raise ScheduleValidationError(
                "block_overlap", f"{day}: blocks {prev[2]} and {cur[2]} overlap"
            )

    result: list[BlockType] = []
    for begin, end, rng, values in parsed:
        r_begin = _round_to_unit(begin, unit_time)
        r_end = _round_to_unit(end, unit_time)
        if r_begin == r_end:
            warnings.append(
                f"{day}: block {rng} shorter than {unit_time} minutes dropped"
            )
            continue
        if r_begin != begin or r_end != end:
            warnings.append(
                f"{day}: block {rng} aligned to {_fmt_range(r_begin, r_end)} "
                f"({unit_time} minutes granularity)"
            )
        result.append((r_begin, r_end, rng, values))
    return result


def _carry_forward(
    week: "dict[str, list[BlockType]]",
    keys: "Sequence[str]",
    warnings: list[str],
    note: str,
):
    """
    Multi-key mode: a block missing a key takes the value of the previous block
    in time (the first block of a day continues the previous day, monday wraps
    to sunday). Dropped blocks are not in the week so they carry nothing.
    Raises ScheduleValidationError("missing_value") when no block carries a key
    ('note' is appended to its message).
    """
    timeline = [values for day in HA_SCHEDULE_WEEKDAY for *_, values in week[day]]
    for i, key in enumerate(keys):
        # the week wraps: the last value of the week precedes monday
        current = next(
            (values[i] for values in reversed(timeline) if values[i] is not None),
            None,
        )
        if current is None:
            raise ScheduleValidationError(
                "missing_value", f"no block in the week has a '{key}' value{note}"
            )
        count = 0
        for values in timeline:
            if values[i] is None:
                values[i] = current
                count += 1
            else:
                current = values[i]
        if count:
            plural = "s" if count > 1 else ""
            warnings.append(f"{key} carried forward into {count} block{plural}")


def _compile_day(
    day: str,
    blocks: "list[BlockType]",
    off_values: "Sequence[int]",
    *,
    entry_count_max: int,
    entry_count_min: int,
) -> "NativeDayType":
    """Builds the native entries of a single (HA named) weekday: one per aligned
    block and one carrying off_values per uncovered span."""
    entries: NativeDayType = []
    cursor = 0
    for begin, end, _, values in blocks:
        if begin > cursor:
            entries.append([begin - cursor, *off_values])
        entries.append([end - begin, *values])
        cursor = end
    if cursor < MINUTES_PER_DAY:
        entries.append([MINUTES_PER_DAY - cursor, *off_values])

    if entry_count_max and len(entries) > entry_count_max:
        raise ScheduleValidationError(
            "too_many_entries",
            f"{day}: {len(entries)} slots exceed the device limit of "
            f"{entry_count_max} (gaps take a slot each, blocks with the same "
            "temperature count separately)",
        )
    for _ in range(entry_count_min - len(entries)):
        entries.append([0, *entries[0][1:]])
    return entries


def decompile_schedule(
    native: "NativeMappingType",
    *,
    scale: int,
    keys: "Sequence[str]" = (KEY_TEMPERATURE,),
    fahrenheit: bool = False,
    device_fahrenheit: bool = False,
) -> "dict[str, list[dict[str, Any]]]":
    """
    Convert a native device payload into the HA schedule helper representation.

    Only weekdays present in native are emitted (keyed by HA weekday name);
    any other key (channel, section, id, pre, ...) is ignored. Every entry
    becomes a block (equal consecutive values are not merged) except for
    zero-duration ones, which are skipped.
    Each entry value is emitted under its key (in order) in the block data, in °C
    or, with fahrenheit, in °F (as the shortest decimal mapping back to it or,
    with device_fahrenheit, as the whole °F a grid point represents).
    Times are emitted as "HH:MM" strings ("24:00" for end of day).

    Raises ScheduleValidationError("native_invalid") for malformed payloads,
    entries not carrying one value per key included (e.g. mts300 entries with the
    default key).
    """
    shape = f"[minutes, {', '.join(keys)}]"
    result: dict[str, list[dict[str, Any]]] = {}
    for ha_day, mts_day in zip(HA_SCHEDULE_WEEKDAY, MTS_SCHEDULE_WEEKDAY):
        if mts_day not in native:
            continue
        day_entries = native[mts_day]
        if not isinstance(day_entries, (list, tuple)):
            raise ScheduleValidationError(
                "native_invalid", f"{mts_day}: entries must be a list"
            )
        blocks = result[ha_day] = []
        cursor = 0
        for index, entry in enumerate(day_entries):
            if not (
                isinstance(entry, (list, tuple))
                and len(entry) == 1 + len(keys)
                and all(type(value) is int for value in entry)
            ):
                raise ScheduleValidationError(
                    "native_invalid", f"{mts_day}: entry #{index} is not {shape}"
                )
            duration, *values = entry
            if duration < 0 or cursor + duration > MINUTES_PER_DAY:
                raise ScheduleValidationError(
                    "native_invalid",
                    f"{mts_day}: entry #{index} has an invalid duration {duration}",
                )
            if duration == 0:
                continue
            blocks.append(
                {
                    KEY_FROM: _fmt(cursor),
                    KEY_TO: _fmt(cursor + duration),
                    KEY_DATA: {
                        key: _to_helper_unit(
                            value, scale, fahrenheit, device_fahrenheit
                        )
                        for key, value in zip(keys, values)
                    },
                }
            )
            cursor += duration
        if day_entries and cursor != MINUTES_PER_DAY:
            raise ScheduleValidationError(
                "native_invalid",
                f"{mts_day}: entries cover {cursor} minutes "
                f"instead of {MINUTES_PER_DAY}",
            )
    return result


def _native_week(
    native: "NativeMappingType", merge: bool
) -> "list[NativeDayType | None]":
    """The 7 mts weekdays of a native payload (None when missing) without zero
    (or negative) duration entries and, with merge, with consecutive entries
    carrying equal values merged."""
    week: list[NativeDayType | None] = []
    for day in MTS_SCHEDULE_WEEKDAY:
        if (entries := native.get(day)) is not None:
            result: NativeDayType = []
            for duration, *values in entries:
                if duration <= 0:
                    continue
                if merge and result and result[-1][1:] == values:
                    result[-1][0] += duration
                else:
                    result.append([duration, *values])
            entries = result
        week.append(entries)
    return week


def native_equal(a: "NativeMappingType", b: "NativeMappingType") -> bool:
    """
    True when two native payloads run the same schedule: their 7 mts weekdays
    are compared ignoring padding and borders between entries with equal values
    (other keys like channel/section/id are ignored; a weekday present on one
    side only makes them different).
    """
    return _native_week(a, True) == _native_week(b, True)


def native_same(a: "NativeMappingType", b: "NativeMappingType") -> bool:
    """
    True when two native payloads carry the same entries: like native_equal but
    split entries with equal values differ from a single one.
    """
    return _native_week(a, False) == _native_week(b, False)
