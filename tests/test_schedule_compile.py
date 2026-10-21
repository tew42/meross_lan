"""Test the pure helpers.schedule module (HA schedule helper <-> native payload)"""

from datetime import time
import json
import math

import pytest

from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    MTS_SCHEDULE_WEEKDAY,
    ScheduleValidationError,
    compile_schedule,
    decompile_schedule,
    native_equal,
    normalize_native_day,
    normalize_temperature,
    normalize_time,
)

# device limits as set up by the calendar entity for the different families
MTS100 = dict(
    scale=10,
    unit_time=15,
    entry_count_max=6,
    entry_count_min=6,
    min_temp=5.0,
    max_temp=35.0,
    temp_step=0.5,
)
MTS200 = dict(
    scale=10,
    unit_time=15,
    entry_count_max=8,
    entry_count_min=0,
    min_temp=5.0,
    max_temp=35.0,
    temp_step=0.5,
)
MTS960 = dict(
    scale=100,
    unit_time=15,
    entry_count_max=12,
    entry_count_min=0,
    min_temp=0.0,
    max_temp=50.0,
    temp_step=0.5,
)
MTS100_TEMP = dict(scale=10, min_temp=5.0, max_temp=35.0, temp_step=0.5)

# verbatim from emulator_traces/U01234567890123456789012345678912-Kpippo-msh300hk-mts150.csv
# (line 25, Appliance.Hub.Mts100.ScheduleB GETACK, subdevice 03002B0E)
MTS100_TRACE = {
    "id": "03002B0E",
    **{
        d: [[390, 150], [90, 180], [300, 180], [300, 210], [210, 210], [150, 150]]
        for d in ("mon", "tue", "wed", "thu", "fri")
    },
    **{
        d: [[390, 155], [165, 170], [225, 210], [300, 210], [195, 210], [165, 150]]
        for d in ("sat", "sun")
    },
}
# verbatim from emulator_traces/U0123456789012345678901234567891F-Kpippo-mts960.csv
# (line 95, Appliance.Control.Thermostat.ScheduleB GETACK)
MTS960_TRACE = {
    "channel": 0,
    "section": 12,
    "sun": [[1440, 1900]],
    "mon": [[1440, 100]],
    "tue": [[1440, 2800]],
    "wed": [[1440, 100]],
    "thu": [[1440, 100]],
    "fri": [[15, 100], [1425, 2000]],
    "sat": [[1440, 100]],
}


def B(begin, end, temperature=None, **data):
    """Build an HA schedule helper block."""
    if temperature is not None:
        data["temperature"] = temperature
    return {"from": begin, "to": end, "data": data}


FULL_DAY = [
    B("00:00", "06:30", 15),
    B("06:30", "13:00", 18),
    B("13:00", "21:30", 21),
    B("21:30", "24:00", 15),
]
FULL_DAY_MTS100 = [[390, 150], [390, 180], [510, 210], [150, 150], [0, 150], [0, 150]]


def week(blocks=FULL_DAY, **overrides):
    """Build a full helper week with the same blocks, overriding some days."""
    result = {day: list(blocks) for day in HA_SCHEDULE_WEEKDAY}
    result.update(overrides)
    return result


def raises_code(code: str, func, *args, **kwargs) -> ScheduleValidationError:
    with pytest.raises(ScheduleValidationError) as excinfo:
        func(*args, **kwargs)
    error = excinfo.value
    assert error.code == code, str(error)
    for value in error.placeholders.values():
        assert isinstance(value, str)
    return error


# --------------------------------------------------------------------------
# normalize_time
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, expected",
    [
        ("06:30", 390),
        ("6:30", 390),
        (" 06:30 ", 390),
        ("06:30:30", 390.5),
        ("00:00", 0),
        ("23:59", 1439),
        ("24:00", 1440),
        ("24:00:00", 1440),
        ("23:59:59.999999", 1440),
        (time(6, 30), 390),
        (time(0, 0), 0),
        (time.max, 1440),
    ],
)
def test_normalize_time(value, expected):
    assert normalize_time(value) == expected


@pytest.mark.parametrize(
    "value",
    ["25:00", "06:60", "24:30", "24:00:01", "6h30", 630, 6.5, None, ""],
)
def test_normalize_time_invalid(value):
    error = raises_code("time_invalid", normalize_time, value)
    assert error.placeholders == {"value": repr(value)}


# --------------------------------------------------------------------------
# normalize_temperature
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, kwargs, expected, warns",
    [
        (21.5, MTS100_TEMP, 215, False),
        (21.3, MTS100_TEMP, 215, True),
        (21.2, MTS100_TEMP, 210, True),
        ("21.5", MTS100_TEMP, 215, False),
        (21, MTS100_TEMP, 210, False),
        (40, MTS100_TEMP, 350, True),
        (2, MTS100_TEMP, 50, True),
        (21.5, dict(MTS100_TEMP, scale=100), 2150, False),
        (1.0, dict(scale=100, min_temp=0.0, max_temp=50.0, temp_step=0.5), 100, False),
        (21.3, dict(MTS100_TEMP, temp_step=0), 213, False),
    ],
)
def test_normalize_temperature(value, kwargs, expected, warns):
    scaled, warning = normalize_temperature(value, **kwargs)
    assert scaled == expected
    assert type(scaled) is int
    assert (warning is not None) == warns


def test_normalize_temperature_warning_text():
    _, warning = normalize_temperature(40, **MTS100_TEMP)
    assert warning == "temperature 40 adjusted to 35"


@pytest.mark.parametrize("value", [True, False, "warm", math.nan, math.inf, None])
def test_normalize_temperature_invalid(value):
    error = raises_code(
        "temperature_invalid", normalize_temperature, value, **MTS100_TEMP
    )
    assert error.placeholders == {"value": repr(value)}


# --------------------------------------------------------------------------
# compile_schedule: happy paths
# --------------------------------------------------------------------------
def test_compile_full_day_mts100():
    result = compile_schedule(week(), **MTS100)
    assert result.warnings == []
    assert list(result.native) == list(MTS_SCHEDULE_WEEKDAY)
    for day in MTS_SCHEDULE_WEEKDAY:
        assert result.native[day] == FULL_DAY_MTS100
        assert sum(entry[0] for entry in result.native[day]) == 1440
    # padding entries must be fresh lists (no aliasing)
    mon = result.native["mon"]
    assert mon[4] is not mon[5]
    assert mon[4] is not mon[0]
    assert result.native["mon"] is not result.native["tue"]


def test_compile_full_day_mts200_no_padding():
    result = compile_schedule(week(), **MTS200)
    assert result.native["mon"] == [[390, 150], [390, 180], [510, 210], [150, 150]]


def test_compile_merges_touching_equal_temperatures():
    blocks = [
        B("00:00", "06:00", 15),
        B("06:00", "08:00", 21),
        B("08:00", "12:00", 21),
        B("12:00", "24:00", 15),
    ]
    result = compile_schedule(week(blocks), **MTS200)
    assert result.native["mon"] == [[360, 150], [360, 210], [720, 150]]


def test_compile_unsorted_input():
    shuffled = [FULL_DAY[2], FULL_DAY[0], FULL_DAY[3], FULL_DAY[1]]
    assert (
        compile_schedule(week(shuffled), **MTS100).native
        == compile_schedule(week(), **MTS100).native
    )


def test_compile_time_objects():
    blocks = [
        B(time(0, 0), time(6, 30), 15),
        B(time(6, 30), time(13, 0), 18),
        B(time(13, 0), time(21, 30), 21),
        B(time(21, 30), time.max, 15),
    ]
    result = compile_schedule(week(blocks), **MTS100)
    assert result.warnings == []
    assert result.native["mon"] == FULL_DAY_MTS100


def test_compile_setback_fills_gaps():
    blocks = [B("06:30", "08:00", 21), B("17:00", "22:00", 21)]
    result = compile_schedule(week(blocks), gap_temperature=15, **MTS100)
    assert result.warnings == []
    assert result.native["mon"] == [
        [390, 150],
        [90, 210],
        [540, 150],
        [300, 210],
        [120, 150],
        [0, 150],
    ]


def test_compile_setback_missing_days():
    result = compile_schedule({"monday": FULL_DAY}, gap_temperature=15, **MTS100)
    assert result.native["mon"] == FULL_DAY_MTS100
    for day in MTS_SCHEDULE_WEEKDAY[1:]:
        assert result.native[day] == [[1440, 150]] + [[0, 150]] * 5


def test_compile_setback_gap_merges_with_equal_neighbour():
    blocks = [B("00:00", "06:00", 15), B("08:00", "24:00", 21)]
    result = compile_schedule(week(blocks), gap_temperature=15, **MTS200)
    assert result.native["mon"] == [[480, 150], [960, 210]]


def test_compile_setback_gap_temperature_warning():
    result = compile_schedule(week(), gap_temperature=2, **MTS100)
    assert result.warnings == ["gap temperature 2 adjusted to 5"]


def test_compile_mts960_no_padding():
    result = compile_schedule(week(), **MTS960)
    assert result.warnings == []
    for day in MTS_SCHEDULE_WEEKDAY:
        assert result.native[day] == [
            [390, 1500],
            [390, 1800],
            [510, 2100],
            [150, 1500],
        ]


def test_compile_entry_count_max_unlimited():
    blocks = [B(f"{h:02d}:00", f"{h + 1:02d}:00", 15 + (h % 2)) for h in range(20)]
    blocks.append(B("20:00", "24:00", 21))
    kwargs = dict(MTS200, entry_count_max=0)
    result = compile_schedule(week(blocks), **kwargs)
    assert len(result.native["mon"]) == 21
    assert sum(entry[0] for entry in result.native["mon"]) == 1440


def test_compile_block_temperature_default():
    blocks = [B("00:00", "06:00"), B("06:00", "24:00", 21)]
    result = compile_schedule(week(blocks), block_temperature_default=17, **MTS200)
    assert result.native["mon"] == [[360, 170], [1080, 210]]
    # also for blocks with no data at all
    blocks = [{"from": "00:00", "to": "06:00"}, B("06:00", "24:00", 21)]
    result = compile_schedule(week(blocks), block_temperature_default=17, **MTS200)
    assert result.native["mon"] == [[360, 170], [1080, 210]]


def test_compile_custom_temperature_key():
    blocks = [
        {"from": "00:00", "to": "12:00", "data": {"setpoint": 18}},
        {"from": "12:00", "to": "24:00", "data": {"setpoint": 21}},
    ]
    result = compile_schedule(week(blocks), temperature_key="setpoint", **MTS200)
    assert result.native["sun"] == [[720, 180], [720, 210]]


def test_compile_weekday_keys_case_insensitive():
    helper = {
        (day.upper() if i % 2 else f" {day.title()} "): FULL_DAY
        for i, day in enumerate(HA_SCHEDULE_WEEKDAY)
    }
    result = compile_schedule(helper, **MTS100)
    assert all(result.native[day] == FULL_DAY_MTS100 for day in MTS_SCHEDULE_WEEKDAY)


def test_compile_json_serializable():
    result = compile_schedule(week(), **MTS100)
    assert json.loads(json.dumps(result.native)) == result.native


def test_compile_temperature_warnings():
    blocks = [B("00:00", "12:00", 21.3), B("12:00", "24:00", 40)]
    result = compile_schedule({"monday": blocks}, gap_temperature=15, **MTS200)
    assert result.native["mon"] == [[720, 215], [720, 350]]
    assert result.warnings == [
        "monday: block 00:00-12:00: temperature 21.3 adjusted to 21.5",
        "monday: block 12:00-24:00: temperature 40 adjusted to 35",
    ]


# --------------------------------------------------------------------------
# compile_schedule: alignment to unit_time
# --------------------------------------------------------------------------
def test_compile_rounding_nearest():
    blocks = [
        B("00:00", "06:10", 15),
        B("06:10", "08:05", 21),
        B("08:05", "24:00", 15),
    ]
    # strict mode: touching blocks stay touching (no day_gap)
    result = compile_schedule(week(blocks), **MTS100)
    assert result.native["mon"] == [
        [375, 150],
        [105, 210],
        [960, 150],
        [0, 150],
        [0, 150],
        [0, 150],
    ]
    monday_warnings = [w for w in result.warnings if w.startswith("monday:")]
    assert monday_warnings == [
        "monday: block 00:00-06:10 aligned to 00:00-06:15 (15 minutes granularity)",
        "monday: block 06:10-08:05 aligned to 06:15-08:00 (15 minutes granularity)",
        "monday: block 08:05-24:00 aligned to 08:00-24:00 (15 minutes granularity)",
    ]


@pytest.mark.parametrize("boundary, expected", [("07:07:30", 435), ("07:07:29", 420)])
def test_compile_rounding_half_up(boundary, expected):
    blocks = [B("00:00", boundary, 15), B(boundary, "24:00", 21)]
    result = compile_schedule(week(blocks), **MTS200)
    assert result.native["mon"] == [[expected, 150], [1440 - expected, 210]]


def test_compile_rounding_drops_short_block():
    blocks = [
        B("00:00", "06:00", 15),
        B("06:00", "06:05", 21),
        B("06:05", "24:00", 15),
    ]
    result = compile_schedule(week(blocks), **MTS100)
    assert result.native["mon"] == [[1440, 150]] + [[0, 150]] * 5
    assert (
        "monday: block 06:00-06:05 shorter than 15 minutes dropped" in result.warnings
    )


def test_compile_rounding_keeps_block_over_half_unit():
    blocks = [
        B("00:00", "06:00", 15),
        B("06:00", "06:08", 21),
        B("06:08", "24:00", 15),
    ]
    result = compile_schedule(week(blocks), **MTS200)
    assert result.native["mon"] == [[360, 150], [15, 210], [1065, 150]]


def test_compile_rounding_closes_short_gap():
    blocks = [B("00:00", "06:00", 15), B("06:05", "24:00", 21)]
    result = compile_schedule(week(blocks), **MTS200)
    assert result.native["mon"] == [[360, 150], [1080, 210]]


def test_compile_unit_time_1_strict_gap():
    blocks = [B("00:00", "23:59", 15)]
    error = raises_code(
        "day_gap", compile_schedule, week(blocks), **dict(MTS200, unit_time=1)
    )
    assert error.placeholders == {"weekday": "monday", "range": "23:59-24:00"}


@pytest.mark.parametrize("unit_time", [7, 0, -15])
def test_compile_bad_unit_time(unit_time):
    with pytest.raises(ValueError):
        compile_schedule(week(), **dict(MTS100, unit_time=unit_time))


# --------------------------------------------------------------------------
# compile_schedule: errors
# --------------------------------------------------------------------------
def test_error_day_gap():
    helper = week(tuesday=[B("06:30", "24:00", 15)])
    error = raises_code("day_gap", compile_schedule, helper, **MTS100)
    assert error.placeholders == {"weekday": "tuesday", "range": "00:00-06:30"}
    assert str(error) == "tuesday: 00:00-06:30 is not covered by any block"


def test_error_day_gap_after_rounding():
    helper = week(monday=[B("00:00", "08:00", 15), B("08:10", "24:00", 21)])
    error = raises_code("day_gap", compile_schedule, helper, **MTS100)
    assert error.placeholders == {"weekday": "monday", "range": "08:00-08:15"}


@pytest.mark.parametrize("empty", ["missing", None, []])
def test_error_day_empty(empty):
    helper = week()
    if empty == "missing":
        del helper["wednesday"]
    else:
        helper["wednesday"] = empty
    error = raises_code("day_empty", compile_schedule, helper, **MTS100)
    assert error.placeholders == {"weekday": "wednesday"}


def test_error_block_overlap():
    helper = week(monday=[B("06:30", "24:00", 21), B("00:00", "07:00", 15)])
    error = raises_code("block_overlap", compile_schedule, helper, **MTS100)
    assert error.placeholders == {
        "weekday": "monday",
        "range_a": "00:00-07:00",
        "range_b": "06:30-24:00",
    }


@pytest.mark.parametrize(
    "begin, end, rng",
    [("06:00", "06:00", "06:00-06:00"), ("08:00", "06:00", "08:00-06:00")],
)
def test_error_block_invalid_range(begin, end, rng):
    helper = week(monday=[B(begin, end, 15)])
    error = raises_code("block_invalid_range", compile_schedule, helper, **MTS100)
    assert error.placeholders == {"weekday": "monday", "range": rng}


@pytest.mark.parametrize(
    "block",
    [
        {"from": "00:00", "data": {"temperature": 15}},
        {"to": "24:00", "data": {"temperature": 15}},
        "00:00-24:00",
        ["00:00", "24:00", 15],
        None,
    ],
)
def test_error_block_invalid(block):
    helper = week(friday=[B("00:00", "06:00", 15), block])
    error = raises_code("block_invalid", compile_schedule, helper, **MTS100)
    assert error.placeholders == {"weekday": "friday", "index": "1"}


@pytest.mark.parametrize("day_value", ["00:00-24:00", 42, {"from": "00:00"}])
def test_error_day_not_a_list(day_value):
    helper = week(friday=day_value)
    error = raises_code("block_invalid", compile_schedule, helper, **MTS100)
    assert error.placeholders["weekday"] == "friday"


def test_error_time_invalid_in_block():
    helper = week(monday=[B("00:00", "25:00", 15)])
    error = raises_code("time_invalid", compile_schedule, helper, **MTS100)
    assert error.placeholders == {
        "weekday": "monday",
        "index": "0",
        "value": repr("25:00"),
    }
    assert str(error).startswith("monday: block #0: ")


@pytest.mark.parametrize(
    "block",
    [{"from": "00:00", "to": "24:00"}, {"from": "00:00", "to": "24:00", "data": {}}],
)
def test_error_block_missing_temperature(block):
    helper = week(monday=[block])
    error = raises_code("block_missing_temperature", compile_schedule, helper, **MTS100)
    assert error.placeholders == {
        "weekday": "monday",
        "range": "00:00-24:00",
        "key": "temperature",
    }


def test_error_temperature_invalid_in_block():
    helper = week(monday=[B("00:00", "24:00", "warm")])
    error = raises_code("temperature_invalid", compile_schedule, helper, **MTS100)
    assert error.placeholders == {
        "weekday": "monday",
        "range": "00:00-24:00",
        "value": repr("warm"),
    }
    assert str(error).startswith("monday: block 00:00-24:00: ")


def test_error_too_many_entries():
    blocks = [B(f"{h:02d}:00", f"{h + 1:02d}:00", 15 + (h % 2)) for h in range(6)] + [
        B("06:00", "24:00", 21)
    ]
    helper = week(monday=blocks)
    error = raises_code("too_many_entries", compile_schedule, helper, **MTS100)
    assert error.placeholders == {"weekday": "monday", "count": "7", "max": "6"}


def test_error_too_many_entries_after_gap_fill():
    blocks = [
        B("06:00", "08:00", 21),
        B("12:00", "14:00", 21),
        B("18:00", "22:00", 21),
    ]
    error = raises_code(
        "too_many_entries",
        compile_schedule,
        week(blocks),
        gap_temperature=15,
        **MTS100,
    )
    assert error.placeholders == {"weekday": "monday", "count": "7", "max": "6"}


def test_error_unknown_weekday():
    helper = {"mon": FULL_DAY}
    error = raises_code("unknown_weekday", compile_schedule, helper, **MTS100)
    assert error.placeholders == {"weekday": "mon"}


def test_error_bad_limits():
    with pytest.raises(ValueError):
        compile_schedule(week(), **dict(MTS100, min_temp=40.0))
    with pytest.raises(ValueError):
        compile_schedule(week(), **dict(MTS100, entry_count_min=8))


def test_error_is_value_error():
    with pytest.raises(ValueError):
        compile_schedule({"mon": FULL_DAY}, **MTS100)


# --------------------------------------------------------------------------
# decompile_schedule
# --------------------------------------------------------------------------
def test_decompile_mts100_trace():
    helper = decompile_schedule(MTS100_TRACE, scale=10)
    assert list(helper) == list(HA_SCHEDULE_WEEKDAY)
    expected_weekday = [
        {"from": "00:00", "to": "06:30", "data": {"temperature": 15.0}},
        {"from": "06:30", "to": "13:00", "data": {"temperature": 18.0}},
        {"from": "13:00", "to": "21:30", "data": {"temperature": 21.0}},
        {"from": "21:30", "to": "24:00", "data": {"temperature": 15.0}},
    ]
    expected_weekend = [
        {"from": "00:00", "to": "06:30", "data": {"temperature": 15.5}},
        {"from": "06:30", "to": "09:15", "data": {"temperature": 17.0}},
        {"from": "09:15", "to": "21:15", "data": {"temperature": 21.0}},
        {"from": "21:15", "to": "24:00", "data": {"temperature": 15.0}},
    ]
    for day in HA_SCHEDULE_WEEKDAY[:5]:
        assert helper[day] == expected_weekday
    for day in HA_SCHEDULE_WEEKDAY[5:]:
        assert helper[day] == expected_weekend
    assert "id" not in helper


def test_decompile_mts960_trace():
    helper = decompile_schedule(MTS960_TRACE, scale=100)
    assert helper["friday"] == [
        {"from": "00:00", "to": "00:15", "data": {"temperature": 1.0}},
        {"from": "00:15", "to": "24:00", "data": {"temperature": 20.0}},
    ]
    assert helper["monday"] == [
        {"from": "00:00", "to": "24:00", "data": {"temperature": 1.0}}
    ]
    assert helper["tuesday"] == [
        {"from": "00:00", "to": "24:00", "data": {"temperature": 28.0}}
    ]
    assert set(helper) == set(HA_SCHEDULE_WEEKDAY)


def test_decompile_skips_zero_duration():
    native = {"mon": [[720, 150], [720, 210], [0, 150], [0, 150]]}
    assert decompile_schedule(native, scale=10) == {
        "monday": [
            {"from": "00:00", "to": "12:00", "data": {"temperature": 15.0}},
            {"from": "12:00", "to": "24:00", "data": {"temperature": 21.0}},
        ]
    }


def test_decompile_time_format():
    helper = decompile_schedule(MTS100_TRACE, scale=10, time_format="time")
    monday = helper["monday"]
    assert [(b["from"], b["to"]) for b in monday] == [
        (time(0, 0), time(6, 30)),
        (time(6, 30), time(13, 0)),
        (time(13, 0), time(21, 30)),
        (time(21, 30), time.max),
    ]


def test_decompile_partial_week():
    native = {"mon": [[1440, 150]], "tue": []}
    assert decompile_schedule(native, scale=10) == {
        "monday": [{"from": "00:00", "to": "24:00", "data": {"temperature": 15.0}}],
        "tuesday": [],
    }


def test_decompile_custom_temperature_key():
    helper = decompile_schedule(
        {"mon": [[1440, 150]]}, scale=10, temperature_key="setpoint"
    )
    assert helper["monday"][0]["data"] == {"setpoint": 15.0}


@pytest.mark.parametrize(
    "day, code",
    [
        ([[720, 150], [710, 210]], "native_invalid"),
        ([[-10, 150], [1450, 210]], "native_invalid"),
        ([[720, 150], [730, 210]], "native_invalid"),
        ([[1440, 150, 200]], "unsupported_entry_width"),
        ([[1440]], "native_invalid"),
        ([None], "native_invalid"),
        ([["a", 150]], "native_invalid"),
        (None, "native_invalid"),
    ],
)
def test_decompile_invalid(day, code):
    error = raises_code(code, decompile_schedule, {"mon": day}, scale=10)
    assert error.placeholders["weekday"] == "mon"


def test_decompile_json_serializable():
    helper = decompile_schedule(MTS100_TRACE, scale=10)
    assert json.loads(json.dumps(helper)) == helper


# --------------------------------------------------------------------------
# round trips
# --------------------------------------------------------------------------
def test_roundtrip_mts100_trace():
    result = compile_schedule(decompile_schedule(MTS100_TRACE, scale=10), **MTS100)
    assert result.warnings == []
    assert native_equal(result.native, MTS100_TRACE)
    assert result.native["mon"] == FULL_DAY_MTS100
    assert result.native["sat"] == [
        [390, 155],
        [165, 170],
        [720, 210],
        [165, 150],
        [0, 155],
        [0, 155],
    ]
    for day in MTS_SCHEDULE_WEEKDAY:
        assert len(result.native[day]) == 6


def test_roundtrip_mts960_trace():
    result = compile_schedule(decompile_schedule(MTS960_TRACE, scale=100), **MTS960)
    assert result.warnings == []
    assert result.native == {d: MTS960_TRACE[d] for d in MTS_SCHEDULE_WEEKDAY}


@pytest.mark.parametrize(
    "trace, kwargs", [(MTS100_TRACE, MTS100), (MTS960_TRACE, MTS960)]
)
def test_roundtrip_time_objects(trace, kwargs):
    scale = kwargs["scale"]
    assert (
        compile_schedule(
            decompile_schedule(trace, scale=scale, time_format="time"), **kwargs
        ).native
        == compile_schedule(decompile_schedule(trace, scale=scale), **kwargs).native
    )


@pytest.mark.parametrize("kwargs", [MTS100, MTS200, MTS960])
def test_roundtrip_idempotent(kwargs):
    blocks = [
        B("00:00", "06:10", 15),
        B("06:10", "08:05", 21.3),
        B("08:05", "24:00", 15),
    ]
    first = compile_schedule(week(blocks), **kwargs).native
    second = compile_schedule(
        decompile_schedule(first, scale=kwargs["scale"]), **kwargs
    )
    assert second.native == first
    assert second.warnings == []


# --------------------------------------------------------------------------
# native_equal / normalize_native_day
# --------------------------------------------------------------------------
def _native(**overrides):
    result = {day: [[720, 150], [720, 210]] for day in MTS_SCHEDULE_WEEKDAY}
    result.update(overrides)
    return result


def test_native_equal_ignores_padding():
    assert native_equal(
        _native(), _native(mon=[[720, 150], [720, 210], [0, 150], [0, 150]])
    )


def test_native_equal_split_equal_temperatures():
    assert native_equal(_native(), _native(mon=[[360, 150], [360, 150], [720, 210]]))


def test_native_equal_ignores_extra_keys():
    assert native_equal(_native(), {**_native(), "id": "03002B0E", "channel": 0})


def test_native_equal_temperature_differs():
    assert not native_equal(_native(), _native(mon=[[720, 150], [720, 215]]))


def test_native_equal_split_point_differs():
    assert not native_equal(_native(), _native(mon=[[705, 150], [735, 210]]))


def test_native_equal_day_missing_one_side():
    other = _native()
    del other["sun"]
    assert not native_equal(_native(), other)
    assert not native_equal(other, _native())


def test_native_equal_day_missing_both_sides():
    a = _native()
    b = _native()
    del a["sun"]
    del b["sun"]
    assert native_equal(a, b)


def test_native_equal_tuples_vs_lists():
    assert native_equal(
        _native(), {day: ((720, 150), (720, 210)) for day in MTS_SCHEDULE_WEEKDAY}
    )


def test_native_equal_traces():
    assert native_equal(MTS100_TRACE, MTS100_TRACE)
    assert not native_equal(MTS100_TRACE, MTS960_TRACE)


def test_normalize_native_day_width_agnostic():
    day = [[300, 200, 250], [0, 180, 260], [300, 200, 250], [840, 180, 260]]
    assert normalize_native_day(day) == [[600, 200, 250], [840, 180, 260]]
    # input is not mutated
    assert day[0] == [300, 200, 250]
