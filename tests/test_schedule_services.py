"""Test the meross_lan.get_schedule / set_schedule / sync_schedule entity services"""

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

try:
    import probatio as vol
except ImportError:
    import voluptuous as vol

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.helpers.schedule import (
    MTS_SCHEDULE_WEEKDAY,
    decompile_schedule,
    native_equal,
)
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect as Select,
    ScheduleSyncSensor as Sensor,
)
import emulator as emulator_module

from tests import const as tc, helpers
from tests.schedule_helpers import (
    HELPER_ENTITY_ID,
    SCHEDULE_FULL,
    async_schedule_context,
)

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant

MTS300_TRACE = "U01234567890123456789012345678925-Kpippo-mts300.json.txt"

# verbatim from the msh300 trace (Appliance.Hub.Mts100.ScheduleB GETACK, subdevice 01008C11).
# The emulator trace import merges these lists by their first item (duration) so that
# its initial state is not a valid schedule: tests set it explicitly.
_MTS100_DAY = [[15, 180], [45, 180], [300, 230], [360, 180], [360, 240], [360, 180]]
MTS100_TRACE: "dict[str, Any]" = {
    day: [list(entry) for entry in _MTS100_DAY] for day in MTS_SCHEDULE_WEEKDAY
}
MTS100_TRACE["wed"] = [
    [15, 105],
    [45, 105],
    [300, 230],
    [360, 105],
    [360, 240],
    [360, 105],
]


async def test_schedule_services_registered(request, hass: "HomeAssistant"):
    async with helpers.DeviceContext(request, hass, mc.TYPE_MSH300, auto_poll=True):
        for service in (
            mlc.SERVICE_GET_SCHEDULE,
            mlc.SERVICE_SET_SCHEDULE,
            mlc.SERVICE_SYNC_SCHEDULE,
        ):
            assert hass.services.has_service(mlc.DOMAIN, service)


async def test_get_schedule(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        await ctx.async_set_device_schedule(MTS100_TRACE)
        ns_value = climate.schedule.ns_value
        assert native_equal(ns_value, MTS100_TRACE)

        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response
        result: "dict[str, Any]" = response[climate.entity_id]  # type: ignore
        assert result.keys() == {"schedule", "limits", "linked_helper"}

        assert result["schedule"] == decompile_schedule(
            ns_value, scale=climate.temperature_scale
        )
        monday = result["schedule"]["monday"]
        assert monday == [
            {"from": "00:00", "to": "01:00", "data": {"temperature": 18.0}},
            {"from": "01:00", "to": "06:00", "data": {"temperature": 23.0}},
            {"from": "06:00", "to": "12:00", "data": {"temperature": 18.0}},
            {"from": "12:00", "to": "18:00", "data": {"temperature": 24.0}},
            {"from": "18:00", "to": "24:00", "data": {"temperature": 18.0}},
        ]
        for block in monday:
            assert isinstance(block["data"]["temperature"], float)
        assert result["schedule"]["wednesday"][0]["data"]["temperature"] == 10.5

        assert result["limits"] == {
            "unit_time": 15,
            "entry_count_max": 6,
            "entry_count_min": 6,
            "temperature_scale": climate.temperature_scale,
            "min_temp": climate.min_temp,
            "max_temp": climate.max_temp,
            "temp_step": climate.target_temperature_step,
        }
        assert result["linked_helper"] is None

        # the 'schedule' key can be pasted back as set_schedule input
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=result["schedule"]
        )
        assert native_equal(ctx.emulator_schedule(), ns_value)

        # unsupported device payload (3-value entries like mts300): device data,
        # not a user input error
        await ctx.async_set_device_schedule(
            {day: [[720, 180, 200], [720, 200, 220]] for day in MTS_SCHEDULE_WEEKDAY}
        )
        with pytest.raises(HomeAssistantError) as excinfo:
            await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert not isinstance(excinfo.value, ServiceValidationError)


async def test_set_schedule(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        schedule = climate.schedule
        expected = ctx.compile(SCHEDULE_FULL)
        assert not native_equal(ctx.emulator_schedule(), expected)

        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=SCHEDULE_FULL
        )
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == expected
        assert expected["mon"] == [
            [390, 160],
            [90, 210],
            [540, 180],
            [330, 215],
            [90, 160],
            [0, 160],
        ]
        # the device schedule is queried back after the write
        assert native_equal(schedule.ns_value, expected)
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response[climate.entity_id]["schedule"]["monday"] == SCHEDULE_FULL["monday"]  # type: ignore

        # a linked helper which differs reports drift (and is not re-written)
        await ctx.select.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 2
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=SCHEDULE_FULL
        )
        assert len(ctx.set_payloads) == 3
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 3
        assert ctx.emulator_schedule() == expected


async def test_set_schedule_mts200(request, hass: "HomeAssistant"):
    """Thermostat.Schedule (mts200: 'section' carries the slots per day)."""
    emulator = helpers.build_emulator(mc.TYPE_MTS200)
    # the trace carries no schedule: seed one
    ns = mn_t.Appliance_Control_Thermostat_Schedule
    emulator.namespaces[ns] = {
        ns.key: [
            {mc.KEY_CHANNEL: 0, mc.KEY_SECTION: 8}
            | {day: [[1440, 200]] + [[0, 200]] * 7 for day in MTS_SCHEDULE_WEEKDAY}
        ]
    }
    async with async_schedule_context(request, hass, emulator) as ctx:
        climate = ctx.climate
        assert climate.SCHEDULE_NS is ns
        assert climate.schedule.limits["entry_count_max"] == 8
        expected = ctx.compile(SCHEDULE_FULL)
        await ctx.async_call(
            mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=SCHEDULE_FULL
        )
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == expected
        assert native_equal(climate.schedule.ns_value, expected)
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response[climate.entity_id]["schedule"] == SCHEDULE_FULL  # type: ignore


async def test_set_schedule_invalid(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        entity_id = ctx.climate.entity_id
        initial_state = deepcopy(ctx.emulator_schedule())

        # gap
        with pytest.raises(ServiceValidationError, match="not covered"):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE,
                entity_id,
                schedule=SCHEDULE_FULL
                | {
                    "monday": [
                        {"from": "00:00", "to": "06:00", "data": {"temperature": 16}},
                        {"from": "07:00", "to": "24:00", "data": {"temperature": 21}},
                    ]
                },
            )

        # 7 distinct blocks on mts100 (scheduleBMode: 6)
        temperatures = (16, 17, 18, 19, 20, 21, 22)
        with pytest.raises(ServiceValidationError, match="exceed the device limit"):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE,
                entity_id,
                schedule=SCHEDULE_FULL
                | {
                    "tuesday": [
                        {
                            "from": f"{i * 3:02d}:00",
                            "to": f"{(i + 1) * 3:02d}:00" if i < 6 else "24:00",
                            "data": {"temperature": t},
                        }
                        for i, t in enumerate(temperatures)
                    ]
                },
            )

        # missing temperature
        with pytest.raises(ServiceValidationError, match="temperature"):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE,
                entity_id,
                schedule=SCHEDULE_FULL | {"friday": [{"from": "00:00", "to": "24:00"}]},
            )

        # rejected by the service schema:
        # partial week (every day is needed)
        with pytest.raises(vol.Invalid, match="sunday"):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE,
                entity_id,
                schedule={
                    day: blocks
                    for day, blocks in SCHEDULE_FULL.items()
                    if day != "sunday"
                },
            )
        # invalid block (missing 'to')
        with pytest.raises(vol.Invalid):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE,
                entity_id,
                schedule=SCHEDULE_FULL
                | {"monday": [{"from": "00:00", "data": {"temperature": 16}}]},
            )
        # unknown weekday
        with pytest.raises(vol.Invalid):
            await ctx.async_call(
                mlc.SERVICE_SET_SCHEDULE,
                entity_id,
                schedule=SCHEDULE_FULL | {"mon": [{"from": "00:00", "to": "24:00"}]},
            )

        # nothing was written
        assert not ctx.set_payloads
        assert ctx.emulator_schedule() == initial_state


async def test_sync_schedule(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        with pytest.raises(ServiceValidationError, match="No schedule helper linked"):
            await ctx.async_call(mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)

        select = ctx.select
        await select.async_select_option(HELPER_ENTITY_ID)
        # the service runs now (no debounce)
        await ctx.async_call(mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)
        assert len(ctx.set_payloads) == 1
        expected = ctx.compile(await select._async_read_helper(HELPER_ENTITY_ID))
        assert ctx.emulator_schedule() == expected
        assert native_equal(climate.schedule.ns_value, expected)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        # forced: written even if unchanged
        await ctx.async_call(mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)
        assert len(ctx.set_payloads) == 2
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response[climate.entity_id]["linked_helper"] == HELPER_ENTITY_ID  # type: ignore


async def test_sync_schedule_offline(request, hass: "HomeAssistant"):
    """A forced sync deferred because the device is offline: the service fails
    and the (still forced) retry writes once the device is back."""
    async with async_schedule_context(request, hass) as ctx:
        climate = ctx.climate
        await ctx.select.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        expected = ctx.emulator_schedule()
        await ctx.async_set_device_schedule(
            expected | {"mon": [[720, 200], [720, 220]] + [[0, 220]] * 4}
        )
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT

        with patch.object(ctx.context.device, "is_connected", False):
            with pytest.raises(HomeAssistantError, match="sync will be retried"):
                await ctx.async_call(mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)
            assert ctx.sensor_state().state == Sensor.STATE_PENDING
        assert len(ctx.set_payloads) == 1
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule() == expected
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_services_unsupported(request, hass: "HomeAssistant"):
    """mts300 (3-value schedule entries) is not supported."""
    emulator = emulator_module.build_emulator(
        tc.EMULATOR_TRACES_PATH + MTS300_TRACE,
        key=tc.MOCK_KEY,
        uuid=tc.MOCK_DEVICE_UUID,
    )
    async with async_schedule_context(request, hass, emulator) as ctx:
        climate = ctx.climate
        assert climate.select_schedule_helper is None
        for service, data in (
            (mlc.SERVICE_GET_SCHEDULE, {}),
            (mlc.SERVICE_SET_SCHEDULE, {"schedule": SCHEDULE_FULL}),
            (mlc.SERVICE_SYNC_SCHEDULE, {}),
        ):
            with pytest.raises(ServiceValidationError, match="not supported"):
                await ctx.async_call(service, climate.entity_id, **data)
