"""Test the meross_lan.get_schedule / set_schedule / sync_schedule entity services"""

from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
import pytest

try:
    import probatio as vol
except:
    import voluptuous as vol

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    MTS_SCHEDULE_WEEKDAY,
    compile_schedule,
    decompile_schedule,
    native_equal,
)
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
)
from custom_components.meross_lan.schedule_sync import ScheduleSyncSensor
import emulator as emulator_module
from emulator.mixins.hub import HubMixin

from tests import const as tc, helpers

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant

MTS300_TRACE = "U01234567890123456789012345678925-Kpippo-mts300.json.txt"

HELPER_ENTITY_ID = "schedule.heating"

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

# a full-coverage week (strings as accepted by the service schema)
SCHEDULE_FULL: "dict[str, Any]" = {
    day: [
        {"from": "00:00", "to": "06:30", "data": {"temperature": 16}},
        {"from": "06:30", "to": "08:00", "data": {"temperature": 21}},
        {"from": "08:00", "to": "17:00", "data": {"temperature": 18}},
        {"from": "17:00", "to": "22:30", "data": {"temperature": 21.5}},
        {"from": "22:30", "to": "24:00", "data": {"temperature": 16}},
    ]
    for day in HA_SCHEDULE_WEEKDAY
}


def _get_mts100(context: helpers.DeviceContext):
    climate = next(
        subdevice
        for subdevice in context.device.subdevices.values()  # type: ignore
        if isinstance(subdevice, MtsClimate)
    )
    emulator = context.emulator
    assert isinstance(emulator, HubMixin)
    return climate, emulator


def _emulator_schedule(emulator: HubMixin, climate: MtsClimate):
    return emulator._get_subdevice_namespace(
        climate.id, mn_h.Appliance_Hub_Mts100_ScheduleB
    )


async def _async_set_device_schedule(
    context: helpers.DeviceContext, climate: MtsClimate, native: "dict[str, Any]"
):
    """Sets the emulated device schedule and refreshes the climate (calendar) state."""
    emulator_state = _emulator_schedule(context.emulator, climate)  # type: ignore
    for day in MTS_SCHEDULE_WEEKDAY:
        emulator_state[day] = [list(entry) for entry in native[day]]
    schedule = climate.schedule
    schedule.handler_ns.schedule_get(schedule.index)
    await context.hass.async_block_till_done()


async def _call(hass: "HomeAssistant", service: str, entity_id: str, **data):
    return await hass.services.async_call(
        mlc.DOMAIN,
        service,
        {"entity_id": entity_id, **data},
        blocking=True,
        return_response=service == mlc.SERVICE_GET_SCHEDULE,
    )


async def test_schedule_services_registered(request, hass: "HomeAssistant"):
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSH300, auto_poll=True
    ) as context:
        for service in (
            mlc.SERVICE_GET_SCHEDULE,
            mlc.SERVICE_SET_SCHEDULE,
            mlc.SERVICE_SYNC_SCHEDULE,
        ):
            assert hass.services.has_service(mlc.DOMAIN, service)


async def test_get_schedule(request, hass: "HomeAssistant"):
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSH300, auto_poll=True
    ) as context:
        climate, emulator = _get_mts100(context)
        await _async_set_device_schedule(context, climate, MTS100_TRACE)
        ns_value = climate.schedule.ns_value
        assert native_equal(ns_value, MTS100_TRACE)

        response = await _call(hass, mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response
        result: "dict[str, Any]" = response[climate.entity_id]  # type: ignore

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

        assert result["native"] == {
            day: ns_value[day] for day in MTS_SCHEDULE_WEEKDAY if day in ns_value
        }
        assert result["limits"] == {
            "unit_time": 15,
            "entry_count_max": 6,
            "entry_count_min": 6,
            "temperature_scale": climate.temperature_scale,
            "min_temp": climate.min_temp,
            "max_temp": climate.max_temp,
            "temp_step": climate.target_temperature_step,
        }
        assert result["schedule_mode_active"] is bool(climate.is_mts_scheduled())
        assert result["linked_helper"] is None

        # the 'schedule' key can be pasted back as set_schedule input
        await _call(
            hass,
            mlc.SERVICE_SET_SCHEDULE,
            climate.entity_id,
            schedule=result["schedule"],
        )
        assert native_equal(_emulator_schedule(emulator, climate), ns_value)

        # unsupported device payload (3-value entries like mts300)
        await _async_set_device_schedule(
            context,
            climate,
            {day: [[720, 180, 200], [720, 200, 220]] for day in MTS_SCHEDULE_WEEKDAY},
        )
        with pytest.raises(ServiceValidationError):
            await _call(hass, mlc.SERVICE_GET_SCHEDULE, climate.entity_id)


async def test_set_schedule(request, hass: "HomeAssistant"):
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSH300, auto_poll=True
    ) as context:
        climate, emulator = _get_mts100(context)
        schedule = climate.schedule
        expected = compile_schedule(
            SCHEDULE_FULL,
            scale=climate.temperature_scale,
            min_temp=climate.min_temp,
            max_temp=climate.max_temp,
            temp_step=climate.target_temperature_step,
            unit_time=schedule._schedule_unit_time,
            entry_count_max=schedule._schedule_entry_count_max,
            entry_count_min=schedule._schedule_entry_count_min,
        ).native
        assert not native_equal(_emulator_schedule(emulator, climate), expected)

        await _call(
            hass, mlc.SERVICE_SET_SCHEDULE, climate.entity_id, schedule=SCHEDULE_FULL
        )
        emulator_state = _emulator_schedule(emulator, climate)
        for day in MTS_SCHEDULE_WEEKDAY:
            assert emulator_state[day] == expected[day]
        assert expected["mon"] == [
            [390, 160],
            [90, 210],
            [540, 180],
            [330, 215],
            [90, 160],
            [0, 160],
        ]

        # the device schedule is refreshed after the write
        assert native_equal(schedule.ns_value, expected)
        response = await _call(hass, mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
        assert response[climate.entity_id]["schedule"]["monday"] == SCHEDULE_FULL["monday"]  # type: ignore

        # a write sets the sync controller reference payload
        assert climate.select_schedule_helper
        assert climate.select_schedule_helper._last_payload == expected


async def test_set_schedule_invalid(request, hass: "HomeAssistant"):
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSH300, auto_poll=True
    ) as context:
        climate, emulator = _get_mts100(context)
        initial_state = deepcopy(_emulator_schedule(emulator, climate))

        # gap
        with pytest.raises(ServiceValidationError, match="not covered"):
            await _call(
                hass,
                mlc.SERVICE_SET_SCHEDULE,
                climate.entity_id,
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
            await _call(
                hass,
                mlc.SERVICE_SET_SCHEDULE,
                climate.entity_id,
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
            await _call(
                hass,
                mlc.SERVICE_SET_SCHEDULE,
                climate.entity_id,
                schedule=SCHEDULE_FULL | {"friday": [{"from": "00:00", "to": "24:00"}]},
            )

        # partial week: every day is needed
        with pytest.raises(ServiceValidationError, match="sunday"):
            await _call(
                hass,
                mlc.SERVICE_SET_SCHEDULE,
                climate.entity_id,
                schedule={
                    day: blocks
                    for day, blocks in SCHEDULE_FULL.items()
                    if day != "sunday"
                },
            )

        # invalid shape (missing 'to'): rejected by the service schema
        with pytest.raises(vol.Invalid):
            await _call(
                hass,
                mlc.SERVICE_SET_SCHEDULE,
                climate.entity_id,
                schedule={"monday": [{"from": "00:00", "data": {"temperature": 16}}]},
            )
        # unknown weekday: rejected by the service schema
        with pytest.raises(vol.Invalid):
            await _call(
                hass,
                mlc.SERVICE_SET_SCHEDULE,
                climate.entity_id,
                schedule={"mon": [{"from": "00:00", "to": "24:00"}]},
            )

        # nothing was written
        assert _emulator_schedule(emulator, climate) == initial_state


async def test_sync_schedule_not_linked(request, hass: "HomeAssistant"):
    async with helpers.DeviceContext(
        request, hass, mc.TYPE_MSH300, auto_poll=True
    ) as context:
        climate, emulator = _get_mts100(context)
        with pytest.raises(ServiceValidationError, match="No schedule helper linked"):
            await _call(hass, mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)


# the schedule helper entity leaves a (time tracking) timer behind
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_sync_schedule(request, hass: "HomeAssistant"):
    """Linked helper happy path: relies on ScheduleHelperSelect.async_sync
    (the sync controller)."""
    assert await async_setup_component(
        hass,
        "schedule",
        {
            "schedule": {
                "heating": {
                    "name": "Heating",
                    **{
                        day: [
                            {
                                "from": block["from"],
                                "to": (
                                    "24:00:00"
                                    if block["to"] == "24:00"
                                    else block["to"]
                                ),
                                "data": block["data"],
                            }
                            for block in blocks
                        ]
                        for day, blocks in SCHEDULE_FULL.items()
                    },
                }
            }
        },
    )
    with (
        patch.object(
            MtsClimate.ScheduleHelperSelect,
            "_attr_entity_registry_enabled_default",
            True,
        ),
        patch.object(
            MtsClimate.ScheduleSyncSensor, "_attr_entity_registry_enabled_default", True
        ),
    ):
        async with helpers.DeviceContext(
            request, hass, mc.TYPE_MSH300, auto_poll=True
        ) as context:
            climate, emulator = _get_mts100(context)
            select = climate.select_schedule_helper
            sensor = climate.sensor_schedule_sync
            assert select and sensor
            assert HELPER_ENTITY_ID in select.options
            await select.async_select_option(HELPER_ENTITY_ID)

            await _call(hass, mlc.SERVICE_SYNC_SCHEDULE, climate.entity_id)

            schedule = climate.schedule
            expected = compile_schedule(
                SCHEDULE_FULL,
                scale=climate.temperature_scale,
                min_temp=climate.min_temp,
                max_temp=climate.max_temp,
                temp_step=climate.target_temperature_step,
                unit_time=schedule._schedule_unit_time,
                entry_count_max=schedule._schedule_entry_count_max,
                entry_count_min=schedule._schedule_entry_count_min,
            ).native
            emulator_state = _emulator_schedule(emulator, climate)
            for day in MTS_SCHEDULE_WEEKDAY:
                assert emulator_state[day] == expected[day]
            assert sensor.native_value != ScheduleSyncSensor.STATE_ERROR

            await context.time_mock.async_tick(select.REFRESH_DELAY)
            assert native_equal(schedule.ns_value, expected)
            assert sensor.native_value == ScheduleSyncSensor.STATE_SYNCED
            response = await _call(hass, mlc.SERVICE_GET_SCHEDULE, climate.entity_id)
            assert response[climate.entity_id]["linked_helper"] == HELPER_ENTITY_ID  # type: ignore


async def test_schedule_services_unsupported(request, hass: "HomeAssistant"):
    """mts300 (3-value schedule entries) is not supported."""
    emulator = emulator_module.build_emulator(
        tc.EMULATOR_TRACES_PATH + MTS300_TRACE,
        key=tc.MOCK_KEY,
        uuid=tc.MOCK_DEVICE_UUID,
    )
    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as context:
        climate = next(
            entity
            for entity in context.device.entities.values()
            if isinstance(entity, MtsClimate)
        )
        assert not climate.SCHEDULE_SYNC_SUPPORTED
        for service, data in (
            (mlc.SERVICE_GET_SCHEDULE, {}),
            (mlc.SERVICE_SET_SCHEDULE, {"schedule": SCHEDULE_FULL}),
            (mlc.SERVICE_SYNC_SCHEDULE, {}),
        ):
            with pytest.raises(ServiceValidationError, match="not supported"):
                await _call(hass, service, climate.entity_id, **data)
