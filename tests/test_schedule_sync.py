"""
Test the schedule helper -> device schedule sync controller (ScheduleHelperSelect)
and its status sensor (ScheduleSyncSensor) on the msh300 hub mts100 valves.
"""

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from homeassistant import const as hac
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.calendar import MtsSchedule
from custom_components.meross_lan.devices.thermostat import (
    MtsSummerMode,
    MtsTempUnit,
)
from custom_components.meross_lan.devices.thermostat.mts960 import Mts960Climate
from custom_components.meross_lan.helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    MTS_SCHEDULE_WEEKDAY,
    compile_schedule,
)
from custom_components.meross_lan.merossclient.protocol import const as mc
from custom_components.meross_lan.merossclient.protocol.namespaces import (
    thermostat as mn_t,
)
from custom_components.meross_lan.schedule_sync import (
    ScheduleHelperSelect as Select,
    ScheduleSyncSensor as Sensor,
    schedule_compile_kwargs,
)
import emulator as emulator_module

from tests import const as tc
from tests.schedule_helpers import (
    HELPER_BLOCKS_MTS300,
    HELPER_ENTITY_ID,
    MTS300_TRACE_DAY,
    MTS300_TRACE_PRE,
    OTHER_DEVICE_UUID,
    OTHER_SCHEDULE,
    OVERLAP_SCHEDULE,
    async_schedule_context,
    async_schedule_device,
    build_mts200_emulator,
    build_mts300_emulator,
    patch_read_helper,
)

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant

    from tests.schedule_helpers import ScheduleContext

SYNCED = (Sensor.STATE_SYNCED, None)


async def _async_link(ctx: "ScheduleContext", select: Select | None = None):
    """Links the helper and lets the (debounced) sync run."""
    await (select or ctx.select).async_select_option(HELPER_ENTITY_ID)
    await ctx.async_tick(Select.SYNC_DELAY)


def _patch_read_error():
    """The helper read fails (helper missing or not available)."""
    return patch.object(
        Select, "_async_read_helper", AsyncMock(side_effect=HomeAssistantError("n/a"))
    )


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
        assert Sensor.ATTR_REASON not in state.attributes
        assert ctx.climate.schedule.ns_value[mc.KEY_ID] == ctx.climate.id

        # (b) helper events without content changes: no writes
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert await select.async_sync() == SYNCED
        assert len(ctx.set_payloads) == 1

        # (c) drift: the device schedule is changed elsewhere
        await ctx.async_drift()
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_DRIFT
        assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_DRIFT
        # drift is flagged, not enforced, also after an error (which device
        # updates don't hide)
        with _patch_read_error():
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        await ctx.async_refresh_device_schedule()
        assert ctx.sensor_state().state == Sensor.STATE_ERROR
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT

        # (d) a forced sync restores the device schedule
        assert await select.async_sync(force=True) == SYNCED
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

        # (f) the helper is removed
        hass.states.async_remove(HELPER_ENTITY_ID)
        await hass.async_block_till_done()
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_ERROR
        assert "was removed" in state.attributes[Sensor.ATTR_REASON]
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_warnings(request, hass: "HomeAssistant"):
    """A helper edit compiling to the same schedule (no write) replaces the
    warnings: the sensor always reflects the last compiled helper."""
    misaligned = {
        day: [
            {"from": "00:00", "to": "07:01", "data": {"temperature": 16}},
            {"from": "07:01", "to": "24:00", "data": {"temperature": 20.5}},
        ]
        for day in HA_SCHEDULE_WEEKDAY
    }
    async with async_schedule_context(request, hass) as ctx:
        with patch_read_helper(misaligned):
            await _async_link(ctx)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert len(state.attributes[Sensor.ATTR_WARNINGS]) == 14
        with patch_read_helper(OTHER_SCHEDULE):  # 07:01 -> 07:00
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 1
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert Sensor.ATTR_WARNINGS not in state.attributes


async def test_schedule_sync_borders(request, hass: "HomeAssistant"):
    """A helper edit only moving block borders between equal temperatures is
    written (the device mirrors the helper blocks) while the drift check ignores
    such differences."""
    # HELPER_BLOCKS with its 21 °C block split in two
    split_schedule = {
        day: [
            {"from": "00:00", "to": "06:30", "data": {"temperature": 15}},
            {"from": "06:30", "to": "08:00", "data": {"temperature": 18}},
            {"from": "08:00", "to": "17:00", "data": {"temperature": 21}},
            {"from": "17:00", "to": "24:00", "data": {"temperature": 21}},
        ]
        for day in HA_SCHEDULE_WEEKDAY
    }
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        unsplit = ctx.emulator_schedule()
        assert unsplit["mon"][:3] == [[390, 150], [90, 180], [960, 210]]

        with patch_read_helper(split_schedule):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            split = ctx.compile(split_schedule)
            assert split["mon"][:4] == [[390, 150], [90, 180], [540, 210], [420, 210]]
            assert ctx.emulator_schedule() == split
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED

            # the device reports the same temperatures with other borders: no drift
            await ctx.async_set_device_schedule(unsplit)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED
            # an explicit selection writes the helper blocks
            await _async_link(ctx, select)
            assert len(ctx.set_payloads) == 3
            assert ctx.emulator_schedule() == split
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_compile_error(request, hass: "HomeAssistant"):
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        emulator_schedule_initial = ctx.emulator_schedule()
        with patch_read_helper(OVERLAP_SCHEDULE):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "overlap" in state.attributes[Sensor.ATTR_REASON]
            assert not ctx.set_payloads
            assert ctx.emulator_schedule() == emulator_schedule_initial
            # a device update doesn't hide the error
            await ctx.async_refresh_device_schedule()
            assert ctx.sensor_state().state == Sensor.STATE_ERROR
            state, reason = await select.async_sync(force=True)
            assert state == Sensor.STATE_ERROR
            assert "overlap" in reason  # type: ignore
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
            assert await select.async_sync(force=True) == (Sensor.STATE_OFF, None)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_OFF

        # re-linking a helper already matching the device: no write
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_offline(request, hass: "HomeAssistant"):
    """The valve is offline while the hub is online: deferred."""
    async with async_schedule_context(request, hass) as ctx:
        assert ctx.context.device.is_connected
        with (
            patch.object(ctx.climate, "is_connected", False),
            patch.object(
                Select,
                "_async_read_helper",
                autospec=True,
                side_effect=Select._async_read_helper,
            ) as read_spy,
        ):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_PENDING
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_OFFLINE
            # climate updates (flushes) don't run it while offline
            for _ in range(3):
                ctx.climate.flush_state()
                await ctx.async_tick(1)
            assert read_spy.await_count == 1
            assert not ctx.set_payloads
        # the device is back: the retry syncs
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_write_error(request, hass: "HomeAssistant"):
    """A write rejected by the device is an error while a failed request is
    retried (on the retry timer only)."""
    async with async_schedule_context(request, hass) as ctx:
        with patch.object(
            MtsSchedule,
            "async_request_payload",
            AsyncMock(
                return_value=SimpleNamespace(
                    method=mc.METHOD_ERROR, payload={mc.KEY_ERROR: {mc.KEY_CODE: -1}}
                )
            ),
        ):
            await _async_link(ctx)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "rejected" in state.attributes[Sensor.ATTR_REASON]
        with patch.object(
            MtsSchedule,
            "async_request_payload",
            AsyncMock(side_effect=Exception("timeout")),
        ) as request_mock:
            assert await ctx.select.async_sync() == (
                Sensor.STATE_PENDING,
                "device write failed: timeout",
            )
            for _ in range(3):
                ctx.climate.flush_state()
                await ctx.async_tick(1)
            assert request_mock.await_count == 1
        assert not ctx.set_payloads
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 1
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


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
        async with ctx.async_hold_writes() as async_write_started:
            await select.async_select_option(HELPER_ENTITY_ID)
            # (not async_tick: it would wait for the held write)
            ctx.context.time_mock.tick(Select.SYNC_DELAY)
            await async_write_started()
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_PENDING
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_WRITING
            await select.async_select_option(hac.STATE_OFF)
            assert ctx.sensor_state().state == Sensor.STATE_OFF

        assert len(ctx.set_payloads) == 1  # the write landed anyway
        assert ctx.sensor_state().state == Sensor.STATE_OFF
        # device updates don't revive the link
        await ctx.async_refresh_device_schedule()
        await ctx.async_tick(Select.RETRY_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_OFF
        assert len(ctx.set_payloads) == 1


async def test_schedule_sync_removed_while_writing(request, hass: "HomeAssistant"):
    """The helper is removed while a write is in progress: the run doesn't
    overwrite the error status when the write completes."""
    async with async_schedule_context(request, hass) as ctx:
        async with ctx.async_hold_writes() as async_write_started:
            await ctx.select.async_select_option(HELPER_ENTITY_ID)
            ctx.context.time_mock.tick(Select.SYNC_DELAY)
            await async_write_started()
            # (no async_block_till_done: it would wait for the held write)
            hass.states.async_remove(HELPER_ENTITY_ID)
            assert ctx.sensor_state().state == Sensor.STATE_ERROR

        assert len(ctx.set_payloads) == 1  # the write landed anyway
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_ERROR
        assert "was removed" in state.attributes[Sensor.ATTR_REASON]
        await ctx.async_refresh_device_schedule()
        assert ctx.sensor_state().state == Sensor.STATE_ERROR


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
            # climate updates (flushes) don't run it before the retry timer
            for _ in range(3):
                ctx.climate.flush_state()
                await ctx.async_tick(1)
            assert len(ctx.set_payloads) == 1
            assert ctx.sensor_state().state == Sensor.STATE_PENDING
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
            with patch.object(ctx.climate, "is_connected", False):
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
    """Restoring the link (HA restart/entry reload) writes the helper only if it
    doesn't compile to the restored reference anymore (edited meanwhile)."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1
        expected = ctx.emulator_schedule()

        # (a) device matching the helper
        await ctx.async_restart()
        assert ctx.select.linked_helper == HELPER_ENTITY_ID
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # (b) device schedule changed elsewhere
        await ctx.async_drift()
        await ctx.async_restart()
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
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

        # (c) helper changed while HA was down: written
        with patch_read_helper(OTHER_SCHEDULE):
            await ctx.async_restart()
            assert len(ctx.set_payloads) == 3
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED

        # (d) no stored reference (first start after an upgrade): adopted
        await ctx.async_restart(stored=False)
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_ha_unit_changed(request, hass: "HomeAssistant"):
    """HA switched to °F: the helper values (no unit) would be read as °F, so the
    sync holds (error, no write, also across a restart) until the helper is
    edited or pushed."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        hass.config.units = US_CUSTOMARY_SYSTEM
        for _ in range(2):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_HA_UNIT
            await ctx.async_restart()
        assert len(ctx.set_payloads) == 1
        # edited in °F: written
        schedule_f = {
            day: [
                {"from": "00:00", "to": "07:00", "data": {"temperature": 61}},
                {"from": "07:00", "to": "24:00", "data": {"temperature": 68}},
            ]
            for day in HA_SCHEDULE_WEEKDAY
        }
        with patch_read_helper(schedule_f):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
        assert len(ctx.set_payloads) == 2
        assert ctx.emulator_schedule()["mon"][:2] == [[420, 160], [1020, 200]]
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED

        # back to °C: held again until a forced push (written as read)
        hass.config.units = METRIC_SYSTEM
        ctx.fire_helper_event()
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_ERROR
        assert len(ctx.set_payloads) == 2
        await ctx.select.async_push()
        assert len(ctx.set_payloads) == 3
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_restore_offline(request, hass: "HomeAssistant"):
    """Restoring the link with the valve offline: the helper is written when the
    valve is back only if it was edited meanwhile."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1

        @contextlib.asynccontextmanager
        async def _async_restore_offline():
            await ctx.async_restart(sync=False)
            with patch.object(ctx.climate, "is_connected", False):
                await ctx.async_tick(Select.SYNC_DELAY)
                state = ctx.sensor_state()
                assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_OFFLINE
                yield
            # back online: the device reports its schedule
            await ctx.async_refresh_device_schedule()
            await ctx.async_tick(1)

        # (a) unchanged helper: adopted
        async with _async_restore_offline():
            pass
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert len(ctx.set_payloads) == 1

        # (b) helper edited meanwhile: written
        with contextlib.ExitStack() as stack:
            async with _async_restore_offline():
                stack.enter_context(patch_read_helper(OTHER_SCHEDULE))
                ctx.fire_helper_event()
                await ctx.async_tick(Select.SYNC_DELAY)
                assert ctx.sensor_state().state == Sensor.STATE_PENDING
            assert len(ctx.set_payloads) == 2
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_restore_errors(request, hass: "HomeAssistant"):
    """Errors while restoring the link: a missing helper (e.g. its integration
    failed to load) stays linked and is adopted when it appears while a compile
    error ends the adoption (the fixed helper is written)."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1

        # (a) missing helper, device schedule changed meanwhile
        await ctx.async_drift()
        helper_state = hass.states.get(HELPER_ENTITY_ID)
        assert helper_state
        hass.states.async_remove(HELPER_ENTITY_ID)
        with _patch_read_error():
            await ctx.async_restart()
        select = ctx.select
        assert select.linked_helper == HELPER_ENTITY_ID
        assert select.options == [hac.STATE_OFF, HELPER_ENTITY_ID]
        assert ctx.sensor_state().state == Sensor.STATE_ERROR
        # it appears: adopted (drift, no write)
        hass.states.async_set(
            HELPER_ENTITY_ID, helper_state.state, helper_state.attributes
        )
        await ctx.async_tick(Select.SYNC_DELAY)
        assert ctx.sensor_state().state == Sensor.STATE_DRIFT
        assert len(ctx.set_payloads) == 1

        # (b) helper not compilable at restore: error, no write. Once fixed
        # (a user edit) it is written
        with patch_read_helper(OVERLAP_SCHEDULE):
            await ctx.async_restart()
            state = ctx.sensor_state()
            assert state.state == Sensor.STATE_ERROR
            assert "overlap" in state.attributes[Sensor.ATTR_REASON]
        assert len(ctx.set_payloads) == 1
        with patch_read_helper(OTHER_SCHEDULE):
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)
            assert len(ctx.set_payloads) == 2
            assert ctx.emulator_schedule() == ctx.compile(OTHER_SCHEDULE)
            assert ctx.sensor_state().state == Sensor.STATE_SYNCED


def _blocks_schedule(count: int) -> "dict[str, Any]":
    """'count' blocks with distinct temperatures on monday (the other days
    continue its last one)."""
    return {
        "monday": [
            {
                "from": f"{i * 3:02d}:00",
                "to": f"{(i + 1) * 3:02d}:00" if i < count - 1 else "24:00",
                "data": {"temperature": 16 + i},
            }
            for i in range(count)
        ]
    }


async def test_schedule_sync_group(request, hass: "HomeAssistant"):
    """A helper shared by thermostats with different limits (mts100 valve: 6 slots
    per day, mts200: 8) is all or nothing: written only if it fits all of them."""
    async with (
        async_schedule_context(request, hass) as ctx,
        async_schedule_device(
            request, hass, build_mts200_emulator(OTHER_DEVICE_UUID)
        ) as ctx_mts200,
    ):
        valve, mts200 = ctx.climate, ctx_mts200.climate
        assert mts200.schedule.limits["entry_count_max"] == 8
        members = ((ctx, valve), (ctx_mts200, mts200))

        def _check(state: str, writes: int):
            for member_ctx, climate in members:
                assert ctx.sensor_state(climate).state == state
                assert len(member_ctx.set_payloads) == writes

        async def _async_edit():
            ctx.fire_helper_event()
            await ctx.async_tick(Select.SYNC_DELAY)

        for _, climate in members:
            await climate.select_schedule_helper.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        _check(Sensor.STATE_SYNCED, 1)

        # (a) 7 slots on monday: written to none, both report the error
        with patch_read_helper(_blocks_schedule(7)):
            await _async_edit()
        _check(Sensor.STATE_ERROR, 1)
        reason = ctx.sensor_state(valve).attributes[Sensor.ATTR_REASON]
        assert "exceed the device limit of 6" in reason
        reason = ctx.sensor_state(mts200).attributes[Sensor.ATTR_REASON]
        assert reason.startswith(f"does not fit {valve.entity_id}: monday")

        # (b) 6 slots: written to both
        six_slots = _blocks_schedule(6)
        with patch_read_helper(six_slots):
            await _async_edit()
        _check(Sensor.STATE_SYNCED, 2)
        for member_ctx, climate in members:
            assert member_ctx.emulator_schedule(climate) == ctx.compile(
                six_slots, climate
            )

        # (c) the valve schedule (limits) is not known: skipped until it reports
        with patch_read_helper(_blocks_schedule(7)):
            with patch.object(type(valve), "schedule_limits_known", return_value=False):
                await _async_edit()
                state = ctx.sensor_state(valve)
                assert state.attributes[Sensor.ATTR_REASON] == Select.REASON_UNKNOWN
            assert ctx.sensor_state(mts200).state == Sensor.STATE_SYNCED
            assert len(ctx_mts200.set_payloads) == 3
            # the valve reports: the helper doesn't fit it
            await ctx.async_refresh_device_schedule(valve)
            await ctx.async_tick(1)
        assert ctx.sensor_state(valve).state == Sensor.STATE_ERROR
        assert len(ctx.set_payloads) == 2
        assert ctx.sensor_state(mts200).state == Sensor.STATE_SYNCED


async def test_schedule_sync_group_mts300(request, hass: "HomeAssistant"):
    """An mts300 (heat/cool) and another model don't share a temperature helper."""
    async with (
        async_schedule_context(request, hass) as ctx,
        async_schedule_device(
            request, hass, build_mts300_emulator(OTHER_DEVICE_UUID)
        ) as ctx_mts300,
    ):
        valve, mts300 = ctx.climate, ctx_mts300.climate
        for climate in (valve, mts300):
            await climate.select_schedule_helper.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        state = ctx.sensor_state(mts300)
        assert state.state == Sensor.STATE_ERROR
        assert "'heat'" in state.attributes[Sensor.ATTR_REASON]
        state = ctx.sensor_state(valve)
        assert state.state == Sensor.STATE_ERROR
        assert state.attributes[Sensor.ATTR_REASON].startswith(
            f"does not fit {mts300.entity_id}: "
        )
        assert not ctx.set_payloads
        assert not ctx_mts300.set_payloads


async def test_schedule_sync_two_thermostats(request, hass: "HomeAssistant"):
    """Both msh300 valves linked to the same helper."""
    async with async_schedule_context(request, hass) as ctx:
        climates = ctx.climates
        assert len(climates) == 2
        for climate in climates:
            await climate.select_schedule_helper.async_select_option(HELPER_ENTITY_ID)
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
        await climates[0].select_schedule_helper.async_select_option(hac.STATE_OFF)
        assert ctx.sensor_state(climates[0]).state == Sensor.STATE_OFF
        assert ctx.sensor_state(climates[1]).state == Sensor.STATE_SYNCED


async def test_schedule_sync_mts300(request, hass: "HomeAssistant"):
    """mts300 (provisional): heat/cool blocks compiled to [minutes, heat, cold]
    entries ('cool' carried forward where missing) and 'pre' echoed."""
    async with async_schedule_context(
        request, hass, build_mts300_emulator(), HELPER_BLOCKS_MTS300
    ) as ctx:
        climate = ctx.climate
        kwargs = schedule_compile_kwargs(climate, hass)
        assert kwargs["entry_count_max"] == 12  # not reported by the device
        # gaps: heat off at min_temp, cool off at max_temp
        assert kwargs["off_values"] == [
            round(climate.min_temp * 100),
            round(climate.max_temp * 100),
        ]
        # a limit reported by the device wins
        with patch.object(climate.schedule, "_schedule_entry_count_max", 8):
            assert schedule_compile_kwargs(climate, hass)["entry_count_max"] == 8
        assert ctx.emulator_schedule()["mon"] == MTS300_TRACE_DAY
        helper = await ctx.select._async_read_helper(HELPER_ENTITY_ID)
        result = compile_schedule(helper, **kwargs)
        expected_day = [[390, 1700, 2700], [90, 2000, 2700], [960, 1900, 2500]]
        assert result.native == {day: expected_day for day in MTS_SCHEDULE_WEEKDAY}
        warnings = ["cool carried forward into 7 blocks"]
        assert result.warnings == warnings

        await _async_link(ctx)
        assert len(ctx.set_payloads) == 1
        ((set_payload,),) = ctx.set_payloads[0].values()
        assert set_payload["pre"] == MTS300_TRACE_PRE
        assert ctx.emulator_schedule() == result.native
        assert ctx._emulator_ns_state(climate)["pre"] == MTS300_TRACE_PRE
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert state.attributes[Sensor.ATTR_WARNINGS] == warnings


async def test_schedule_sync_mts300_fahrenheit(request, hass: "HomeAssistant"):
    """HA in °F: a helper seeded from get_schedule maps back exactly to the device
    schedule, so linking it or restoring the link writes nothing (no drift)."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    # MTS300_TRACE_DAY in °F
    helper_blocks = [
        {"from": "00:00:00", "to": "06:00:00", "data": {"heat": 70, "cool": 79}},
        {"from": "06:00:00", "to": "21:00:00", "data": {"heat": 70, "cool": 74.98}},
        {"from": "21:00:00", "to": "24:00:00", "data": {"heat": 70, "cool": 79}},
    ]
    async with async_schedule_context(
        request, hass, build_mts300_emulator(), helper_blocks
    ) as ctx:
        entity_id = ctx.climate.entity_id
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, entity_id)
        monday = response[entity_id]["schedule"]["monday"]  # type: ignore
        assert [block["data"] for block in monday] == [
            block["data"] for block in helper_blocks
        ]

        await _async_link(ctx)
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_SYNCED
        assert Sensor.ATTR_WARNINGS not in state.attributes
        assert not ctx.set_payloads

        await ctx.async_restart()
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        assert not ctx.set_payloads
        assert ctx.emulator_schedule()["mon"] == MTS300_TRACE_DAY


async def test_schedule_sync_races(request, hass: "HomeAssistant"):
    """A push superseded by a link change fails and a push on the removed select
    (e.g. disabled) fails."""
    async with async_schedule_context(request, hass) as ctx:
        select = ctx.select
        await _async_link(ctx)
        async with ctx.async_hold_writes() as async_write_started:
            push = hass.async_create_task(select.async_push())
            await async_write_started()
            await select.async_select_option(HELPER_ENTITY_ID)
        with pytest.raises(HomeAssistantError, match="superseded"):
            await push

        await select.async_remove()
        with pytest.raises(HomeAssistantError, match="select is disabled"):
            await select.async_push()


async def test_schedule_sync_device_fahrenheit(request, hass: "HomeAssistant"):
    """A helper shared by a °C valve and a °F mts200 (display unit): each is
    written on its own grid (0.5 °C, whole °F truncated to the device scale) and
    both are synced. A device unit switch reruns the sync: one write."""
    async with (
        async_schedule_context(request, hass) as ctx,
        async_schedule_device(
            request,
            hass,
            build_mts200_emulator(OTHER_DEVICE_UUID, mc.TEMPUNIT_FAHRENHEIT),
        ) as ctx_mts200,
    ):
        valve, mts200 = ctx.climate, ctx_mts200.climate
        for climate in (valve, mts200):
            await climate.select_schedule_helper.async_select_option(HELPER_ENTITY_ID)
        await ctx.async_tick(Select.SYNC_DELAY)
        # HELPER_BLOCKS: 15, 18, 21 °C
        assert ctx.emulator_schedule(valve)["mon"][:3] == [
            [390, 150],
            [90, 180],
            [960, 210],
        ]
        # 59 °F, 64 °F (17.78 °C), 70 °F (21.11 °C)
        assert ctx_mts200.emulator_schedule(mts200)["mon"][:3] == [
            [390, 150],
            [90, 177],
            [960, 211],
        ]
        for member_ctx, climate in ((ctx, valve), (ctx_mts200, mts200)):
            assert ctx.sensor_state(climate).state == Sensor.STATE_SYNCED
            assert len(member_ctx.set_payloads) == 1
        response = await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, mts200.entity_id)
        limits = response[mts200.entity_id]["limits"]  # type: ignore
        assert (limits["device_unit"], limits["temp_step"]) == ("°F", 1)

        # the mts200 is switched to °C: written on the 0.5 °C grid
        unit_select = next(
            entity
            for entity in mts200.parent.entities.values()
            if isinstance(entity, MtsTempUnit)
        )
        await unit_select.async_select_option(hac.UnitOfTemperature.CELSIUS)
        await ctx.async_tick(1)
        assert len(ctx_mts200.set_payloads) == 2
        assert ctx_mts200.emulator_schedule(mts200)["mon"][:3] == [
            [390, 150],
            [90, 180],
            [960, 210],
        ]
        assert ctx.sensor_state(mts200).state == Sensor.STATE_SYNCED
        # ...and back to °F: every value moves to the °F grid (also those the
        # device holds)
        await unit_select.async_select_option(hac.UnitOfTemperature.FAHRENHEIT)
        await ctx.async_tick(1)
        assert len(ctx_mts200.set_payloads) == 3
        assert ctx_mts200.emulator_schedule(mts200)["mon"][:3] == [
            [390, 150],
            [90, 177],
            [960, 211],
        ]
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx_mts200.set_payloads) == 3
        assert len(ctx.set_payloads) == 1
        # an unknown device unit defers the sync (and get_schedule fails)
        with patch.object(unit_select, "ns_value", None):
            assert not mts200.schedule_limits_known()
            with pytest.raises(HomeAssistantError, match="not available yet"):
                await ctx.async_call(mlc.SERVICE_GET_SCHEDULE, mts200.entity_id)


# a 07:00-22:00 block: the rest of the day is off
GAP_BLOCKS = [{"from": "07:00:00", "to": "22:00:00", "data": {"temperature": 21}}]


def _check_gaps(ctx: "ScheduleContext", writes: int, off: float):
    """The GAP_BLOCKS helper is synced with its gaps at 'off' (°C)."""
    assert len(ctx.set_payloads) == writes
    scale = ctx.climate.temperature_scale
    off = round(off * scale)
    assert ctx.emulator_schedule()["mon"][:3] == [
        [420, off],
        [900, 21 * scale],
        [120, off],
    ]
    assert ctx.sensor_state().state == Sensor.STATE_SYNCED


async def test_schedule_sync_mts960(request, hass: "HomeAssistant"):
    """mts960: the time not covered by the helper is off by the working mode
    (heat: min_temp, cool: max_temp). A working mode or bounds change reruns the
    sync: one write each."""
    emulator = emulator_module.build_emulator(
        tc.EMULATOR_TRACES_PATH + "U0123456789012345678901234567891F-Kpippo-mts960.csv",
        key=tc.MOCK_KEY,
        uuid=tc.MOCK_DEVICE_UUID,
    )
    async with async_schedule_context(request, hass, emulator, GAP_BLOCKS) as ctx:
        climate = ctx.climate
        await climate.async_set_preset_mode(Mts960Climate.Preset.SCHEDULE_HEATING)
        await _async_link(ctx)
        _check_gaps(ctx, 1, climate.min_temp)
        await climate.async_set_preset_mode(Mts960Climate.Preset.SCHEDULE_COOLING)
        await ctx.async_tick(1)
        _check_gaps(ctx, 2, climate.max_temp)
        # the device bounds change
        ns = mn_t.Appliance_Control_Thermostat_CtlRange
        ctx.emulator.namespaces[ns][ns.key][0][mc.KEY_CTLMAX] = 3000
        await climate.parent.ns_handlers[ns].async_get(climate.index)
        await ctx.async_tick(1)
        assert climate.max_temp == 30
        _check_gaps(ctx, 3, 30)
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 3


async def test_schedule_sync_mts200_summer_mode(request, hass: "HomeAssistant"):
    """mts200: the time not covered by the helper is off by the summer mode (off:
    min_temp, on/cooling: max_temp). Switching it reruns the sync: one write."""
    emulator = build_mts200_emulator(summer_mode=mc.MTS200_SUMMERMODE_HEAT)
    async with async_schedule_context(request, hass, emulator, GAP_BLOCKS) as ctx:
        climate = ctx.climate
        await _async_link(ctx)
        _check_gaps(ctx, 1, climate.min_temp)
        summer_mode = next(
            entity
            for entity in climate.parent.entities.values()
            if isinstance(entity, MtsSummerMode)
        )
        await summer_mode.async_turn_on()
        await ctx.async_tick(1)
        _check_gaps(ctx, 2, climate.max_temp)
        await ctx.async_tick(Select.RETRY_DELAY)
        assert len(ctx.set_payloads) == 2
        # an unknown summer mode (e.g. reconnecting) defers the sync
        with patch.object(summer_mode, "ns_value", None):
            assert not climate.schedule_limits_known()


async def test_schedule_sync_select_removed(request, hass: "HomeAssistant"):
    """The select removed (e.g. disabled): the sync status is off."""
    async with async_schedule_context(request, hass) as ctx:
        await _async_link(ctx)
        assert ctx.sensor_state().state == Sensor.STATE_SYNCED
        await ctx.select.async_remove()
        state = ctx.sensor_state()
        assert state.state == Sensor.STATE_OFF
        assert Sensor.ATTR_HELPER not in state.attributes
