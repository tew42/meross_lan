"""Shared setup for the schedule helper sync and schedule services tests."""

import contextlib
from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.setup import async_setup_component

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    MTS_SCHEDULE_WEEKDAY,
    compile_schedule,
)
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect,
    ScheduleSyncSensor,
    schedule_compile_kwargs,
)

from tests import helpers

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant
    from pytest import FixtureRequest

    from emulator import Emulator

HELPER_ENTITY_ID = "schedule.heating"
# the helper (YAML) blocks, the same for every day
HELPER_BLOCKS = [
    {"from": "00:00:00", "to": "06:30:00", "data": {"temperature": 15}},
    {"from": "06:30:00", "to": "08:00:00", "data": {"temperature": 18}},
    {"from": "08:00:00", "to": "24:00:00", "data": {"temperature": 21}},
]
# helper-formatted schedules (as returned by schedule.get_schedule or as
# accepted by meross_lan.set_schedule)
OTHER_SCHEDULE: "dict[str, Any]" = {
    day: [
        {"from": "00:00", "to": "07:00", "data": {"temperature": 16}},
        {"from": "07:00", "to": "24:00", "data": {"temperature": 20.5}},
    ]
    for day in HA_SCHEDULE_WEEKDAY
}
GAP_SCHEDULE: "dict[str, Any]" = {
    day: [
        {"from": "00:00", "to": "07:00", "data": {"temperature": 16}},
        {"from": "08:00", "to": "24:00", "data": {"temperature": 20}},
    ]
    for day in HA_SCHEDULE_WEEKDAY
}
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


def patch_read_helper(ha_schedule: "dict[str, Any]"):
    """Patches the helper read (the HA schedule helper content)."""

    async def _async_read_helper(self, entity_id, /):
        return ha_schedule

    return patch.object(ScheduleHelperSelect, "_async_read_helper", _async_read_helper)


class ScheduleContext:
    """Test state holder: the device context and its thermostats. The entities
    are looked up on access so that they're still valid after an entry reload."""

    def __init__(self, context: helpers.DeviceContext, /):
        self.context = context
        self.hass = context.hass
        self.emulator = context.emulator
        self.set_payloads: list["dict[str, Any]"] = []
        """Schedule SET payloads received by the emulator."""

    @property
    def climates(self) -> "list[MtsClimate]":
        device = self.context.device
        entities = (
            *device.entities.values(),
            *getattr(device, "subdevices", {}).values(),
        )
        # dict: keep the order, drop duplicates
        return list({id(e): e for e in entities if isinstance(e, MtsClimate)}.values())

    @property
    def climate(self):
        return self.climates[0]

    @property
    def select(self) -> ScheduleHelperSelect:
        return self.climate.select_schedule_helper  # type: ignore[return-value]

    def sensor_state(self, climate: MtsClimate | None = None):
        sensor = (climate or self.climate).sensor_schedule_sync
        assert sensor
        state = self.hass.states.get(sensor.entity_id)
        assert state
        return state

    def _emulator_ns_state(self, climate: MtsClimate) -> "dict[str, Any]":
        ns = climate.SCHEDULE_NS
        ((index_key, index_value),) = climate.schedule.index.items()
        return next(
            p
            for p in self.emulator.namespaces[ns][ns.key]
            if p[index_key] == index_value
        )

    def emulator_schedule(self, climate: MtsClimate | None = None):
        """The (emulated) device schedule (weekdays only)."""
        p_state = self._emulator_ns_state(climate or self.climate)
        return {day: p_state[day] for day in MTS_SCHEDULE_WEEKDAY}

    async def async_set_device_schedule(
        self, native: "dict[str, Any]", climate: MtsClimate | None = None
    ):
        """Changes the emulated device schedule (as if done in the Meross app)
        and lets the climate (calendar) entity know about it."""
        climate = climate or self.climate
        p_state = self._emulator_ns_state(climate)
        for day in MTS_SCHEDULE_WEEKDAY:
            p_state[day] = [list(entry) for entry in native[day]]
        await self.async_refresh_device_schedule(climate)

    async def async_refresh_device_schedule(self, climate: MtsClimate | None = None):
        schedule = (climate or self.climate).schedule
        await schedule.handler_ns.async_get(schedule.index)
        await self.hass.async_block_till_done()

    def compile(self, ha_schedule, climate: MtsClimate | None = None):
        return compile_schedule(
            ha_schedule, **schedule_compile_kwargs(climate or self.climate)
        ).native

    async def async_tick(self, delay: float):
        await self.context.time_mock.async_tick(delay)

    def fire_helper_event(self):
        """Re-writes the helper state unchanged (a state_reported event)."""
        state = self.hass.states.get(HELPER_ENTITY_ID)
        assert state
        self.hass.states.async_set(HELPER_ENTITY_ID, state.state, state.attributes)

    async def async_call(self, service: str, entity_id: str, **data):
        return await self.hass.services.async_call(
            mlc.DOMAIN,
            service,
            {"entity_id": entity_id, **data},
            blocking=True,
            return_response=service == mlc.SERVICE_GET_SCHEDULE,
        )


@contextlib.asynccontextmanager
async def async_schedule_context(
    request: "FixtureRequest",
    hass: "HomeAssistant",
    device: "Emulator | str" = mc.TYPE_MSH300,
):
    """Sets up the 'schedule.heating' helper and the device (with the schedule
    helper select and sync sensor enabled) spying the schedule SETs."""
    assert await async_setup_component(
        hass,
        "schedule",
        {
            "schedule": {
                "heating": {"name": "Heating"}
                | {day: HELPER_BLOCKS for day in HA_SCHEDULE_WEEKDAY}
            }
        },
    )
    try:
        with (
            patch.object(
                ScheduleHelperSelect, "_attr_entity_registry_enabled_default", True
            ),
            patch.object(
                ScheduleSyncSensor, "_attr_entity_registry_enabled_default", True
            ),
        ):
            async with helpers.DeviceContext(
                request, hass, device, auto_poll=True
            ) as context:
                ctx = ScheduleContext(context)
                emulator_class = type(ctx.emulator)
                _handle_message = emulator_class._handle_message
                schedule_namespaces = {climate.SCHEDULE_NS for climate in ctx.climates}

                def _handle_message_spy(emulator, header, payload, /):
                    if (header[mc.KEY_METHOD] == mc.METHOD_SET) and (
                        header[mc.KEY_NAMESPACE] in schedule_namespaces
                    ):
                        ctx.set_payloads.append(payload)
                    return _handle_message(emulator, header, payload)

                with patch.object(
                    emulator_class, "_handle_message", _handle_message_spy
                ):
                    yield ctx
    finally:
        # remove the helper so that its (state update) timer doesn't linger
        if helper := hass.data[DATA_INSTANCES]["schedule"].get_entity(HELPER_ENTITY_ID):
            await helper.async_remove()
