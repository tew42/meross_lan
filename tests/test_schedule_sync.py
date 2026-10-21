"""
Test the schedule helper -> device schedule sync controller (ScheduleHelperSelect)
and its status sensor (ScheduleSyncSensor) on a hub mts100 valve.
"""

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from homeassistant import const as hac
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.setup import async_setup_component

from custom_components.meross_lan.climate import MtsClimate
from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    MTS_SCHEDULE_WEEKDAY,
    compile_schedule,
)
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    hub as mn_h,
)
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect,
    ScheduleSyncSensor,
    async_refresh_device_schedule,
    schedule_compile_kwargs,
)
from emulator.mixins.hub import HubMixin

from tests import helpers

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant

HELPER_ENTITY_ID = "schedule.heating"
BLOCKS = [
    {"from": "00:00:00", "to": "06:30:00", "data": {"temperature": 15}},
    {"from": "06:30:00", "to": "08:00:00", "data": {"temperature": 18}},
    {"from": "08:00:00", "to": "24:00:00", "data": {"temperature": 21}},
]
OTHER_SCHEDULE = {
    day: [
        {"from": "00:00", "to": "07:00", "data": {"temperature": 16}},
        {"from": "07:00", "to": "24:00", "data": {"temperature": 20.5}},
    ]
    for day in HA_SCHEDULE_WEEKDAY
}
GAP_SCHEDULE = {
    day: [
        {"from": "00:00", "to": "07:00", "data": {"temperature": 16}},
        {"from": "08:00", "to": "24:00", "data": {"temperature": 20}},
    ]
    for day in HA_SCHEDULE_WEEKDAY
}

SCHEDULE_NS = mn_h.Appliance_Hub_Mts100_ScheduleB


class _Context:
    """Test state holder."""

    def __init__(self, context: helpers.DeviceContext, hass: "HomeAssistant"):
        self.context = context
        self.hass = hass
        emulator = context.emulator
        assert isinstance(emulator, HubMixin)
        self.emulator = emulator
        self.climate: MtsClimate = next(
            subdevice
            for subdevice in context.device.subdevices.values()  # type: ignore
            if isinstance(subdevice, MtsClimate)
        )
        self.select: ScheduleHelperSelect = self.climate.select_schedule_helper  # type: ignore
        self.sensor: ScheduleSyncSensor = self.climate.sensor_schedule_sync  # type: ignore
        assert self.select.hass_connected and self.sensor.hass_connected
        self.set_payloads: list["dict[str, Any]"] = []

    @property
    def emulator_schedule(self):
        """The (emulated) device schedule state of the climate."""
        p_state = self.emulator._get_subdevice_namespace(self.climate.id, SCHEDULE_NS)
        return {day: p_state[day] for day in MTS_SCHEDULE_WEEKDAY}

    @property
    def sensor_state(self):
        state = self.hass.states.get(self.sensor.entity_id)  # type: ignore
        assert state
        return state

    def compile(self, ha_schedule) -> "dict[str, Any]":
        return compile_schedule(
            ha_schedule, **schedule_compile_kwargs(self.climate)
        ).native

    async def async_tick(self, delay: float):
        await self.context.time_mock.async_tick(delay)

    def fire_helper_event(self):
        """Re-writes the helper state unchanged (a state_reported event)."""
        state = self.hass.states.get(HELPER_ENTITY_ID)
        assert state
        self.hass.states.async_set(HELPER_ENTITY_ID, state.state, state.attributes)


@contextlib.asynccontextmanager
async def _async_setup(request, hass: "HomeAssistant"):
    assert await async_setup_component(
        hass,
        "schedule",
        {
            "schedule": {
                "heating": {"name": "Heating"}
                | {day: BLOCKS for day in HA_SCHEDULE_WEEKDAY}
            }
        },
    )
    with (
        patch.object(
            ScheduleHelperSelect, "_attr_entity_registry_enabled_default", True
        ),
        patch.object(ScheduleSyncSensor, "_attr_entity_registry_enabled_default", True),
    ):
        async with helpers.DeviceContext(
            request, hass, mc.TYPE_MSH300, auto_poll=True
        ) as context:
            ctx = _Context(context, hass)
            emulator_class = type(ctx.emulator)
            _handle_message = emulator_class._handle_message

            def _handle_message_spy(emulator, header, payload, /):
                if (header[mc.KEY_METHOD] == mc.METHOD_SET) and (
                    header[mc.KEY_NAMESPACE] == SCHEDULE_NS
                ):
                    ctx.set_payloads.append(payload)
                return _handle_message(emulator, header, payload)

            try:
                with patch.object(
                    emulator_class, "_handle_message", _handle_message_spy
                ):
                    yield ctx
            finally:
                # remove the helper so that its (state update) timer doesn't linger
                if helper := hass.data[DATA_INSTANCES]["schedule"].get_entity(
                    HELPER_ENTITY_ID
                ):
                    await helper.async_remove()


async def test_schedule_sync(request, hass: "HomeAssistant"):
    async with _async_setup(request, hass) as ctx:
        select = ctx.select
        sensor = ctx.sensor
        assert HELPER_ENTITY_ID in select.options
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_OFF
        # the informative attributes are refreshed on every climate flush
        assert (
            ScheduleSyncSensor.ATTR_SCHEDULE_MODE_ACTIVE
            in sensor.extra_state_attributes
        )
        assert sensor.extra_state_attributes[
            ScheduleSyncSensor.ATTR_TIMEZONE_MISMATCH
        ] in (
            True,
            False,
        )
        emulator_schedule_initial = ctx.emulator_schedule
        expected = ctx.compile(await select._async_read_helper(HELPER_ENTITY_ID))
        assert expected != emulator_schedule_initial

        # (a) link the helper: debounced write then confirmation
        await select.async_select_option(HELPER_ENTITY_ID)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_PENDING
        await ctx.async_tick(select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule == expected
        assert select._last_payload == expected
        state = ctx.sensor_state
        assert state.state == ScheduleSyncSensor.STATE_PENDING
        assert state.attributes[ScheduleSyncSensor.ATTR_REASON] == select.REASON_WAITING
        await ctx.async_tick(select.REFRESH_DELAY)
        state = ctx.sensor_state
        assert state.state == ScheduleSyncSensor.STATE_SYNCED
        assert state.attributes[ScheduleSyncSensor.ATTR_HELPER] == HELPER_ENTITY_ID
        assert ScheduleSyncSensor.ATTR_LAST_SYNC in state.attributes
        assert ScheduleSyncSensor.ATTR_REASON not in state.attributes
        assert ctx.climate.schedule.ns_value[mc.KEY_ID] == ctx.climate.id

        # (b) helper events without config changes: no writes
        ctx.fire_helper_event()
        await ctx.async_tick(select.SYNC_DELAY)
        await ctx.async_tick(select.REFRESH_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED
        assert await select.async_sync() == ScheduleSyncSensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # (d) drift: the device schedule is changed elsewhere
        p_state = ctx.emulator._get_subdevice_namespace(ctx.climate.id, SCHEDULE_NS)
        p_state["mon"] = [
            [720, 200],
            [720, 220],
            [0, 200],
            [0, 200],
            [0, 200],
            [0, 200],
        ]
        await async_refresh_device_schedule(ctx.climate)
        await hass.async_block_till_done()
        state = ctx.sensor_state
        assert state.state == ScheduleSyncSensor.STATE_DRIFT
        assert state.attributes[ScheduleSyncSensor.ATTR_REASON] == select.REASON_DRIFT
        # drift is flagged, not enforced
        ctx.fire_helper_event()
        await ctx.async_tick(select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_DRIFT
        # forced sync restores the device schedule
        assert await select.async_sync(force=True) == ScheduleSyncSensor.STATE_PENDING
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule == expected
        await ctx.async_tick(select.REFRESH_DELAY)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED

        # (c) the helper content changes: new write
        expected_other = ctx.compile(OTHER_SCHEDULE)
        assert expected_other != expected
        with patch.object(
            ScheduleHelperSelect,
            "_async_read_helper",
            AsyncMock(return_value=OTHER_SCHEDULE),
        ):
            ctx.fire_helper_event()
            await ctx.async_tick(select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 3
            assert ctx.emulator_schedule == expected_other
            await ctx.async_tick(select.REFRESH_DELAY)
            assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED
            # a regular device poll confirming the state doesn't touch the sensor
            last_sync = ctx.sensor_state.attributes[ScheduleSyncSensor.ATTR_LAST_SYNC]
            await async_refresh_device_schedule(ctx.climate)
            await hass.async_block_till_done()
            state = ctx.sensor_state
            assert state.state == ScheduleSyncSensor.STATE_SYNCED
            assert state.attributes[ScheduleSyncSensor.ATTR_LAST_SYNC] == last_sync

        # the helper is removed
        hass.states.async_remove(HELPER_ENTITY_ID)
        await hass.async_block_till_done()
        state = ctx.sensor_state
        assert state.state == ScheduleSyncSensor.STATE_ERROR
        assert "was removed" in state.attributes[ScheduleSyncSensor.ATTR_REASON]
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_compile_error(request, hass: "HomeAssistant"):
    async with _async_setup(request, hass) as ctx:
        select = ctx.select
        emulator_schedule_initial = ctx.emulator_schedule
        with patch.object(
            ScheduleHelperSelect,
            "_async_read_helper",
            AsyncMock(return_value=GAP_SCHEDULE),
        ):
            await select.async_select_option(HELPER_ENTITY_ID)
            await ctx.async_tick(select.SYNC_DELAY)
            state = ctx.sensor_state
            assert state.state == ScheduleSyncSensor.STATE_ERROR
            assert "not covered" in state.attributes[ScheduleSyncSensor.ATTR_REASON]
            assert not ctx.set_payloads
            assert ctx.emulator_schedule == emulator_schedule_initial
            # a device poll doesn't hide the error
            await async_refresh_device_schedule(ctx.climate)
            await hass.async_block_till_done()
            assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_ERROR
            assert await select.async_sync(force=True) == ScheduleSyncSensor.STATE_ERROR
            assert not ctx.set_payloads

        # the helper is fixed
        ctx.fire_helper_event()
        await ctx.async_tick(select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        await ctx.async_tick(select.REFRESH_DELAY)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED


async def test_schedule_sync_off(request, hass: "HomeAssistant"):
    async with _async_setup(request, hass) as ctx:
        select = ctx.select
        await select.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(select.SYNC_DELAY)
        await ctx.async_tick(select.REFRESH_DELAY)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        await select.async_select_option(hac.STATE_OFF)
        state = ctx.sensor_state
        assert state.state == ScheduleSyncSensor.STATE_OFF
        assert ScheduleSyncSensor.ATTR_HELPER not in state.attributes
        assert select._last_payload is None
        with patch.object(
            ScheduleHelperSelect,
            "_async_read_helper",
            AsyncMock(return_value=OTHER_SCHEDULE),
        ):
            ctx.fire_helper_event()
            await ctx.async_tick(select.SYNC_DELAY)
            await ctx.async_tick(select.REFRESH_DELAY)
            assert await select.async_sync(force=True) == ScheduleSyncSensor.STATE_OFF
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_OFF

        # re-linking a helper already matching the device: no write
        await select.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED


async def test_schedule_sync_offline(request, hass: "HomeAssistant"):
    async with _async_setup(request, hass) as ctx:
        select = ctx.select
        device = ctx.context.device
        with patch.object(device, "is_connected", False):
            await select.async_select_option(HELPER_ENTITY_ID)
            await ctx.async_tick(select.SYNC_DELAY)
            state = ctx.sensor_state
            assert state.state == ScheduleSyncSensor.STATE_PENDING
            assert state.attributes[ScheduleSyncSensor.ATTR_REASON] == "device offline"
            assert not ctx.set_payloads
        # the device is back: the retry syncs
        await ctx.async_tick(select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 1
        await ctx.async_tick(select.REFRESH_DELAY)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED


async def test_schedule_sync_write_error(request, hass: "HomeAssistant"):
    async with _async_setup(request, hass) as ctx:
        select = ctx.select
        schedule_class = type(ctx.climate.schedule)
        with patch.object(
            schedule_class,
            "async_request_payload",
            AsyncMock(side_effect=Exception("timeout")),
        ):
            await select.async_select_option(HELPER_ENTITY_ID)
            await ctx.async_tick(select.SYNC_DELAY)
            state = ctx.sensor_state
            assert state.state == ScheduleSyncSensor.STATE_ERROR
            assert "timeout" in state.attributes[ScheduleSyncSensor.ATTR_REASON]
        with patch.object(
            schedule_class,
            "async_request_payload",
            AsyncMock(
                return_value=SimpleNamespace(
                    method=mc.METHOD_ERROR, payload={mc.KEY_ERROR: {mc.KEY_CODE: -1}}
                )
            ),
        ):
            assert await select.async_sync() == ScheduleSyncSensor.STATE_ERROR
            assert (
                "rejected"
                in ctx.sensor_state.attributes[ScheduleSyncSensor.ATTR_REASON]
            )
        assert select._last_payload is None
        assert not ctx.set_payloads
        # the next sync retries the write
        assert await select.async_sync() == ScheduleSyncSensor.STATE_PENDING
        assert len(ctx.set_payloads) == 1
        await ctx.async_tick(select.REFRESH_DELAY)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED


async def test_schedule_sync_options(request, hass: "HomeAssistant"):
    """The select options follow the schedule helpers added/removed at runtime."""
    async with _async_setup(request, hass) as ctx:
        select = ctx.select
        await select.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(select.SYNC_DELAY)
        await ctx.async_tick(select.REFRESH_DELAY)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]

        other_entity_id = "schedule.other"
        hass.states.async_set(other_entity_id, hac.STATE_OFF, {})
        await hass.async_block_till_done()
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID, other_entity_id]
        select_state = hass.states.get(select.entity_id)  # type: ignore
        assert select_state
        assert select_state.attributes["options"] == select.options
        assert select_state.state == HELPER_ENTITY_ID

        hass.states.async_remove(other_entity_id)
        await hass.async_block_till_done()
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        await ctx.async_tick(select.SYNC_DELAY)
        assert select.linked_helper == HELPER_ENTITY_ID
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # the linked helper is removed: it stays selected (and listed) while
        # the status reports the error
        helper_state = hass.states.get(HELPER_ENTITY_ID)
        assert helper_state
        hass.states.async_remove(HELPER_ENTITY_ID)
        await hass.async_block_till_done()
        assert select.linked_helper == HELPER_ENTITY_ID
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        assert hass.states.get(select.entity_id).state == HELPER_ENTITY_ID  # type: ignore
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_ERROR

        # ...and it comes back: the existing subscriptions resync
        hass.states.async_set(
            HELPER_ENTITY_ID, helper_state.state, helper_state.attributes
        )
        await hass.async_block_till_done()
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        await ctx.async_tick(select.SYNC_DELAY)
        assert ctx.sensor_state.state == ScheduleSyncSensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1
