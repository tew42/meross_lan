"""
Test the schedule helper -> device schedule sync controller (ScheduleHelperSelect)
and its status sensor (ScheduleSyncSensor) on the msh300 hub mts100 valves.
"""

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from homeassistant import const as hac
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.meross_lan.calendar import MtsSchedule
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect as Select,
    ScheduleSyncSensor as Sensor,
)

from tests.schedule_helpers import (
    GAP_SCHEDULE,
    HELPER_ENTITY_ID,
    OTHER_SCHEDULE,
    async_schedule_context,
    patch_read_helper,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from tests.schedule_helpers import ScheduleContext

DRIFTED_DAY = [[720, 200], [720, 220], [0, 200], [0, 200], [0, 200], [0, 200]]


async def _async_link(ctx: "ScheduleContext", select: Select | None = None):
    """Links the helper and lets the (debounced) sync run."""
    await (select or ctx.select).async_select_option(HELPER_ENTITY_ID)
    await ctx.async_tick(Select.SYNC_DELAY)


async def _async_drift(ctx: "ScheduleContext"):
    """Changes the device schedule elsewhere (as the Meross app would)."""
    await ctx.async_set_device_schedule(ctx.emulator_schedule() | {"mon": DRIFTED_DAY})


async def test_schedule_sync(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        assert HELPER_ENTITY_ID in select.options
        assert ctx.sensor_state().state == Sensor.STATE_OFF
        expected = ctx.compile(await select._async_read_helper(HELPER_ENTITY_ID))
        assert expected != ctx.emulator_schedule()

        # (a) link the helper: debounced write and confirmation
        await select.async_select_option(HELPER_ENTITY_ID)
        assert ctx.sensor_state().state == Sensor.STATE_PENDING
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.emulator_schedule() == expected
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert state.attributes[Sensor.ATTR_HELPER] == HELPER_ENTITY_ID
        assert Sensor.ATTR_LAST_SYNC in state.attributes
        assert Sensor.ATTR_REASON not in state.attributes
        assert ctx.climate.schedule.ns_value[mc.KEY_ID] == ctx.climate.id

        # (b) helper events without content changes: no writes
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert await select.async_sync() == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # (c) drift: the device schedule is changed elsewhere
        await _async_drift(ctx)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_DRIFT
        assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_DRIFT
        # drift is flagged, not enforced
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT

        # (d) a forced sync restores the device schedule
        assert await select.async_sync(force=True) == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule() == expected

        # (e) the helper content changes: new write
        expected_other = ctx.compile(OTHER_SCHEDULE)
        assert expected_other != expected
        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 3
            assert ctx.emulator_schedule() == expected_other
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED
            # a regular device update confirming the state doesn't touch the sensor
            last_sync = ctx.sensor_state().attributes[Sensor.ATTR_LAST_SYNC]
            await ctx.async_refresh_device_schedule()
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_SYNCED
            assert state.attributes[Sensor.ATTR_LAST_SYNC] == last_sync

        # (f) the helper is removed
        hass.states.async_remove(HELPER_ENTITY_ID)
        await hass.async_block_till_done()
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_ERROR
        assert "was removed" in state.attributes[Sensor.ATTR_REASON]
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_compile_error(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        emulator_schedule_initial = ctx.emulator_schedule()
        with patch_read_helper(GAP_SCHEDULE):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "not covered" in state.attributes[Sensor.ATTR_REASON]
            assert not ctx.set_payloads
            assert ctx.emulator_schedule() == emulator_schedule_initial
            # a device update doesn't hide the error
            await ctx.async_refresh_device_schedule()
            assert ctx.sensor_state().state == Sensor.STATE_ERROR
            assert await select.async_sync(force=True) == Sensor.STATE_ERROR
            assert not ctx.set_payloads

        # the helper is fixed
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_off(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        await select.async_select_option(hac.STATE_OFF)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_OFF
        assert Sensor.ATTR_HELPER not in state.attributes
        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert await select.async_sync(force=True) == Sensor.STATE_OFF
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_OFF

        # re-linking a helper already matching the device: no write
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_offline(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        with patch.object(ctx.context.device, "is_connected", False):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_PENDING
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_OFFLINE
            assert not ctx.set_payloads
        # the device is back: the retry syncs
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_write_error(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        with patch.object(
            MtsSchedule,
            "async_request_payload",
            AsyncMock(side_effect=Exception("timeout")),
        ):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "timeout" in state.attributes[Sensor.ATTR_REASON]
        with patch.object(
            MtsSchedule,
            "async_request_payload",
            AsyncMock(
                return_value=SimpleNamespace(
                    method=mc.METHOD_ERROR, payload={mc.KEY_ERROR: {mc.KEY_CODE: -1}}
                )
            ),
        ):
            assert await select.async_sync() == Sensor.STATE_ERROR
            assert "rejected" in ctx.sensor_state().attributes[Sensor.ATTR_REASON]
        assert not ctx.set_payloads
        # the next sync retries the write
        assert await select.async_sync() == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1


async def test_schedule_sync_options(request, hass: "HomeAssistant"):
    """The select options follow the schedule helpers added/removed at runtime."""
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
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
        await ctx.async_tick(Select.SYNC_DELAY)
        assert select.linked_helper == HELPER_ENTITY_ID
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
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
        assert ctx.sensor_state().state == Sensor.STATE_ERROR

        # ...and it comes back: the existing subscriptions resync
        hass.states.async_set(
            HELPER_ENTITY_ID, helper_state.state, helper_state.attributes
        )
        await hass.async_block_till_done()
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1


async def test_schedule_sync_unlink_while_writing(request, hass: "HomeAssistant"):
    """A run superseded while awaiting the device doesn't touch the status."""
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        write_gate = asyncio.Event()
        async_request_payload = MtsSchedule.async_request_payload

        async def _async_request_payload_slow(self, payload, /):
            await write_gate.wait()
            return await async_request_payload(self, payload)

        with patch.object(
            MtsSchedule, "async_request_payload", _async_request_payload_slow
        ):
            await select.async_select_option(HELPER_ENTITY_ID)
            # (not async_tick: it would wait for the blocked write)
            ctx.context.time_mock.tick(Select.SYNC_DELAY)
            for _ in range(10):
                await asyncio.sleep(0)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_PENDING
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_WRITING
            await select.async_select_option(hac.STATE_OFF)
            assert ctx.sensor_state().state == Sensor.STATE_OFF
            write_gate.set()
            await hass.async_block_till_done()

        assert len(ctx.set_payloads) == 1  # the write landed anyway
        assert ctx.sensor_state().state == Sensor.STATE_OFF
        # device updates don't revive the link
        await ctx.async_refresh_device_schedule()
        await ctx.async_tick(Select.RETRY_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_OFF
        assert len(ctx.set_payloads) == 1


async def test_schedule_sync_unconfirmed(request, hass: "HomeAssistant"):
    """The write is acknowledged but the confirmation query fails: retried."""
    async with async_schedule_context(request, hass) as ctx:
        expected = ctx.compile(await ctx.select._async_read_helper(HELPER_ENTITY_ID))
        handler = ctx.climate.schedule.handler_ns
        with patch.object(
            handler, "async_get", AsyncMock(side_effect=Exception("timeout"))
        ):
            await _async_link(ctx)
            assert len(ctx.set_payloads) == 1
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_PENDING
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_CONFIRM
        # the device didn't report its schedule meanwhile: the retry writes again
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 2
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert ctx.emulator_schedule() == expected


async def test_schedule_sync_reconnect(request, hass: "HomeAssistant"):
    """The helper is edited while the device is offline: the sync runs as soon as
    the device reports its schedule (not at the retry timeout)."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        with patch_read_helper(OTHER_SCHEDULE):
            with patch.object(ctx.context.device, "is_connected", False):
                ctx.fire_helper_event()
                await ctx.async_tick(Select.SYNC_DELAY)
                state = ctx.sensor_state()
                assert state.state == Sensor.STATE_PENDING
                assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_OFFLINE
            assert len(ctx.set_payloads) == 1
            # back online: the device reports its (unchanged) schedule
            await ctx.async_refresh_device_schedule()
            await ctx.async_tick(1)
            assert len(ctx.set_payloads) == 2
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_restore(request, hass: "HomeAssistant"):
    """Restoring the link (HA restart/entry reload) never writes the device."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1
        select_entity_id = ctx.select.entity_id
        expected = ctx.emulator_schedule()

        # (a) device matching the helper
        assert await ctx.context.async_unload()
        mock_restore_cache(hass, [State(select_entity_id, HELPER_ENTITY_ID)])
        await ctx.context.perform_coldstart()
        assert ctx.select.linked_helper == HELPER_ENTITY_ID
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # (b) device changed while meross_lan was not running
        p_state = ctx._emulator_ns_state(ctx.climate)
        assert await ctx.context.async_unload()
        p_state["mon"] = [list(entry) for entry in DRIFTED_DAY]
        mock_restore_cache(hass, [State(select_entity_id, HELPER_ENTITY_ID)])
        await ctx.context.perform_coldstart()
        await ctx.async_tick(Select.SYNC_DELAY)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 1
        # helper state updates (block boundaries) don't write either
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 1
        # an explicit selection writes
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule() == expected
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_restore_errors(request, hass: "HomeAssistant"):
    """Errors while restoring the link: a compile error ends the adoption (the
    fixed helper is written) while a helper read failure doesn't."""

    async def _async_restore(ctx: "ScheduleContext", select_entity_id: str):
        assert await ctx.context.async_unload()
        mock_restore_cache(hass, [State(select_entity_id, HELPER_ENTITY_ID)])
        await ctx.context.perform_coldstart()
        await ctx.async_tick(Select.SYNC_DELAY)

    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1
        select_entity_id = ctx.select.entity_id
        expected = ctx.emulator_schedule()

        # (a) helper read failure at restore, then the (unchanged) helper is read:
        # the device already matches, no write
        read_failures = []
        async_read_helper = Select._async_read_helper

        async def _async_read_helper_fail_once(self, entity_id, /):
            if not read_failures:
                read_failures.append(entity_id)
                raise HomeAssistantError("helper not available")
            return await async_read_helper(self, entity_id)

        with patch.object(Select, "_async_read_helper", _async_read_helper_fail_once):
            await _async_restore(ctx, select_entity_id)
            assert read_failures
            assert ctx.sensor_state().state == Sensor.STATE_ERROR
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # (b) same with a device changed meanwhile: still adopted (drift, no write)
        p_state = ctx._emulator_ns_state(ctx.climate)
        read_failures.clear()
        with patch.object(Select, "_async_read_helper", _async_read_helper_fail_once):
            assert await ctx.context.async_unload()
            p_state["mon"] = [list(entry) for entry in DRIFTED_DAY]
            mock_restore_cache(hass, [State(select_entity_id, HELPER_ENTITY_ID)])
            await ctx.context.perform_coldstart()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert ctx.sensor_state().state == Sensor.STATE_ERROR
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 1

        # (c) helper not compilable at restore: error, no write. Once fixed
        # (a user edit) it is written
        with patch_read_helper(GAP_SCHEDULE):
            await _async_restore(ctx, select_entity_id)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "not covered" in state.attributes[Sensor.ATTR_REASON]
        assert len(ctx.set_payloads) == 1
        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.emulator_schedule() != expected
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_two_thermostats(request, hass: "HomeAssistant"):
    """Both msh300 valves linked to the same helper."""
    async with async_schedule_context(request, hass) as ctx:
        climates = ctx.climates
        assert len(climates) == 2
        for climate in climates:
            await climate.select_schedule_helper.async_select_option(  # type: ignore
                HELPER_ENTITY_ID
            )
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 2
        expected = ctx.compile(await ctx.select._async_read_helper(HELPER_ENTITY_ID))
        for climate in climates:
            assert ctx.emulator_schedule(climate) == expected
            assert ctx.sensor_state(climate).state == Sensor.STATE_SYNCED

        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 4
        for climate in climates:
            assert ctx.emulator_schedule(climate) == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state(climate).state == Sensor.STATE_SYNCED

        # unlinking one doesn't affect the other
        await climates[0].select_schedule_helper.async_select_option(  # type: ignore
            hac.STATE_OFF
        )
        assert ctx.sensor_state(climates[0]).state == Sensor.STATE_OFF
        assert ctx.sensor_state(climates[1]).state == Sensor.STATE_SYNCED
