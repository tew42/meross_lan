"""
Entities bridging a HomeAssistant Schedule helper (schedule.*) to the on-device
weekly schedule of Meross thermostats (MtsClimate).
- ScheduleHelperSelect: selects the helper linked to the thermostat and runs
  the one-way sync controller (helper -> device): the helper is compiled and
  written to the device whenever its content changes (or on (re)link/forced sync).
  A device schedule changed elsewhere is flagged as 'drift' (not enforced).
- ScheduleSyncSensor: diagnostic status of the sync.
These are aliased as MtsClimate.ScheduleHelperSelect / MtsClimate.ScheduleSyncSensor
(see climate.py) so that derived climate classes can override them.
"""

import asyncio
from typing import TYPE_CHECKING, override

from homeassistant.core import CoreState, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_added_domain,
    async_track_state_change_event,
    async_track_state_removed_domain,
    async_track_state_report_event,
)
from homeassistant.util import dt as dt_util

from .const import hac
from .helpers.schedule import ScheduleValidationError, compile_schedule, native_equal
from .merossclient.protocol import const as mc
from .merossclient.protocol.namespaces import hub as mn_h
from .select import SelectEntity
from .sensor import EnumSensorEntity

if TYPE_CHECKING:
    from typing import Any, Callable, Final, Iterable, Mapping

    from homeassistant.core import Event

    from .climate import MtsClimate


SCHEDULE_DOMAIN = "schedule"
"""HA core Schedule helper entity domain."""
SCHEDULE_SERVICE_GET = "get_schedule"
"""HA core Schedule helper service returning the helper weekly schedule."""


class ScheduleSyncSensor(EnumSensorEntity):
    """Diagnostic sensor reporting the state of the schedule helper -> device sync."""

    if TYPE_CHECKING:
        native_value: str
        STATE_OFF: Final
        STATE_PENDING: Final
        STATE_SYNCED: Final
        STATE_DRIFT: Final
        STATE_ERROR: Final
        ATTR_HELPER: Final
        ATTR_REASON: Final
        ATTR_WARNINGS: Final
        ATTR_LAST_SYNC: Final
        ATTR_SCHEDULE_MODE_ACTIVE: Final
        ATTR_TIMEZONE_MISMATCH: Final

    _INIT_entity_key = "schedule_sync"

    STATE_OFF = "off"
    """No helper linked."""
    STATE_PENDING = "pending"
    """Sync scheduled/in progress or waiting for the device (offline/confirmation)."""
    STATE_SYNCED = "synced"
    """Device schedule matches the compiled helper."""
    STATE_DRIFT = "drift"
    """Device schedule was changed elsewhere (Meross app, calendar entity...)."""
    STATE_ERROR = "error"
    """Helper not compilable/readable or device write failed (see 'reason')."""

    ATTR_HELPER = "helper"
    ATTR_REASON = "reason"
    ATTR_WARNINGS = "warnings"
    ATTR_LAST_SYNC = "last_sync"
    ATTR_SCHEDULE_MODE_ACTIVE = "schedule_mode_active"
    ATTR_TIMEZONE_MISMATCH = "timezone_mismatch"

    # HA core entity attributes:
    _attr_available = True
    _attr_entity_category = EnumSensorEntity.EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:calendar-sync"
    _unrecorded_attributes = frozenset(
        {
            ATTR_WARNINGS,
            ATTR_REASON,
            *EnumSensorEntity._unrecorded_attributes,
        }
    )

    options: list[str] = [
        STATE_OFF,
        STATE_PENDING,
        STATE_SYNCED,
        STATE_DRIFT,
        STATE_ERROR,
    ]

    __slots__ = ()

    def __init__(self, climate: "MtsClimate", /):
        self.extra_state_attributes = {}
        EnumSensorEntity.__init__(
            self, climate, native_value=ScheduleSyncSensor.STATE_OFF
        )

    @override
    def set_unavailable(self):
        # keep the last status while the device is offline: the sync controller
        # will report 'pending' itself when it needs the device.
        pass

    # interface: self
    def update_status(
        self,
        state: str,
        /,
        *,
        reason: str | None = None,
        warnings: "Iterable[str]" = (),
        helper: str | None = None,
        last_sync: bool = False,
    ):
        """Updates the sync status. Optional attributes are removed when None/empty
        except 'last_sync' which is only (re)set when last_sync=True."""
        attrs = self.extra_state_attributes
        for key, value in (
            (self.ATTR_HELPER, helper),
            (self.ATTR_REASON, reason),
            (self.ATTR_WARNINGS, list(warnings)),
        ):
            if value:
                attrs[key] = value
            else:
                attrs.pop(key, None)
        if last_sync:
            attrs[self.ATTR_LAST_SYNC] = dt_util.utcnow().isoformat()
        self.native_value = state
        self.flush_state()

    def update_device_attributes(
        self, schedule_mode_active: bool, timezone_mismatch: bool, /
    ):
        """Updates the informative device attributes (flushing only on changes)."""
        attrs = self.extra_state_attributes
        if (attrs.get(self.ATTR_SCHEDULE_MODE_ACTIVE) is not schedule_mode_active) or (
            attrs.get(self.ATTR_TIMEZONE_MISMATCH) is not timezone_mismatch
        ):
            attrs[self.ATTR_SCHEDULE_MODE_ACTIVE] = schedule_mode_active
            attrs[self.ATTR_TIMEZONE_MISMATCH] = timezone_mismatch
            self.flush_state()


class ScheduleHelperSelect(SelectEntity.RestoreEntity, SelectEntity):
    """
    A select entity listing the HA Schedule helpers (schedule.*) so that the user
    can link one to the thermostat. The linked helper is compiled into the device
    weekly schedule payload and written to the device whenever it changes (one-way).
    """

    if TYPE_CHECKING:
        SYNC_DELAY: Final[int]
        """Debounce delay before syncing after a helper change/(re)selection."""
        REFRESH_DELAY: Final[int]
        """Delay before querying the device schedule after a write."""
        RETRY_DELAY: Final[int]
        """Retry delay when the device is offline or its schedule limits are unknown."""

        REASON_WAITING: Final[str]
        REASON_DRIFT: Final[str]

        climate: "MtsClimate"
        _sync_lock: asyncio.Lock
        """Serializes sync runs (timers, service calls)."""
        _unsub_started: Callable[[], None] | None
        _unsub_added: Callable[[], None] | None
        """Tracks schedule helpers added to HA (options refresh)."""
        _unsub_removed: Callable[[], None] | None
        """Tracks schedule helpers removed from HA (options refresh)."""
        _unsub_state: Callable[[], None] | None
        _unsub_report: Callable[[], None] | None
        _last_payload: dict[str, Any] | None
        """Last native payload written to the device by us (sync or services)."""
        _last_helper_native: dict[str, Any] | None
        """Last successfully compiled (and written/adopted) helper schedule. This is
        the reference for the drift check. None when not (yet) synced or in error."""
        _last_warnings: tuple[str, ...]
        """Compile warnings of _last_helper_native."""
        _last_ns_value: Any
        """Identity cache of the device schedule state (climate.schedule.ns_value)."""
        _refresh_pending: bool
        """A write was done and the confirmation (GET) is pending."""

    _INIT_entity_key = "schedule_helper"
    SYNC_DELAY = 2
    REFRESH_DELAY = 3
    RETRY_DELAY = 60
    REASON_WAITING = "waiting for device confirmation"
    REASON_DRIFT = "device schedule differs from the linked helper"

    # HA core entity attributes:
    _attr_available = True
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:calendar-sync"

    _INIT__last_warnings = ()
    _INIT__refresh_pending = False
    _AUTO_INIT = (
        "_unsub_started",
        "_unsub_added",
        "_unsub_removed",
        "_unsub_state",
        "_unsub_report",
        "_last_payload",
        "_last_helper_native",
        "_last_warnings",
        "_last_ns_value",
        "_refresh_pending",
    )
    __slots__ = (
        "climate",
        "_sync_lock",
    )

    def __init__(self, climate: "MtsClimate", /):
        self.climate = climate
        self._sync_lock = asyncio.Lock()
        super().__init__(climate)
        # Watch the device schedule state (the calendar flush_state is also
        # invoked on every climate flush). This can't be unregistered and is
        # cleaned up on the calendar shutdown: _on_schedule_state guards itself.
        climate.schedule.register_state_callback(self._on_schedule_state)

    @override
    async def async_shutdown(self):
        self._cancel_started_listener()
        self._options_tracking_stop()
        self._sync_stop()
        await super().async_shutdown()
        del self.climate

    @override
    def set_unavailable(self):
        # Keep the selected option (the base class would reset it): the sync
        # controller manages device disconnections by itself.
        pass

    async def async_added_to_hass(self):
        if not self.current_option and (
            restore_data := self._async_get_restored_data()
        ):
            self.current_option = restore_data.state.state

        if self.hass.state == CoreState.running:
            self._setup_options()
        else:
            self._unsub_started = self.hass.bus.async_listen_once(
                hac.EVENT_HOMEASSISTANT_STARTED,
                self._setup_options,
            )

        # call super after (eventually) calling _setup_options since it
        # could flush the new state (it should only when called by the hass bus)
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self._cancel_started_listener()
        self._options_tracking_stop()
        self._sync_stop()
        await super().async_will_remove_from_hass()

    # interface: SelectEntity
    @override
    async def async_select_option(self, option: str):
        self.update_option(option)
        self._sync_start()

    # interface: self
    @property
    def linked_helper(self) -> str | None:
        """The entity_id of the linked schedule helper (None when not linked)."""
        option = self.current_option
        if option in (
            None,
            hac.STATE_OFF,
            hac.STATE_UNKNOWN,
            hac.STATE_UNAVAILABLE,
        ):
            return None
        return option

    def _build_options(self):
        return [
            hac.STATE_OFF,
            *sorted(self.hass.states.async_entity_ids(SCHEDULE_DOMAIN)),
        ]

    @callback
    def _setup_options(self, *_):
        self._unsub_started = None  # one-shot listener (if any) already fired
        # keep the options updated when schedule helpers are added/removed
        self._options_tracking_stop()
        self._unsub_added = async_track_state_added_domain(
            self.hass, SCHEDULE_DOMAIN, self._on_helpers_changed
        )
        self._unsub_removed = async_track_state_removed_domain(
            self.hass, SCHEDULE_DOMAIN, self._on_helpers_changed
        )
        self.options = self._build_options()
        if self.current_option not in self.options:
            # this might happen when restoring a not anymore valid entity
            if self.linked_helper:
                self.log(
                    self.WARNING,
                    "Linked schedule helper (%s) is missing: unlinking",
                    self.current_option,
                )
            self.current_option = hac.STATE_OFF

        self.flush_state()
        self._sync_start()

    @callback
    def _on_helpers_changed(self, event: "Event[Any]", /):
        """A schedule helper was added to/removed from HA: refresh the options.
        This doesn't touch the link/sync: when the linked helper is removed it stays
        selected (and listed) so that the user sees what is missing (the sync status
        reports the error) and the sync recovers if it comes back."""
        options = self._build_options()
        if (linked_helper := self.linked_helper) and (linked_helper not in options):
            options.append(linked_helper)
        if options != self.options:
            self.options = options
            self.flush_state()

    def _options_tracking_stop(self):
        if self._unsub_added:
            self._unsub_added()
            self._unsub_added = None
        if self._unsub_removed:
            self._unsub_removed()
            self._unsub_removed = None

    def _cancel_started_listener(self):
        if self._unsub_started:
            self._unsub_started()
            self._unsub_started = None

    def _update_status(self, state: str, /, **kwargs: "Any"):
        """Forwards a status update to the sync sensor adding the linked helper.
        Returns the state for convenience."""
        kwargs.setdefault("helper", self.linked_helper)
        self.climate.sensor_schedule_sync.update_status(state, **kwargs)  # type: ignore[union-attr]
        return state

    # sync controller
    def _sync_start(self):
        """(Re)starts the sync controller for the currently linked helper (if any)."""
        self._sync_stop()
        self._last_payload = None
        self._last_helper_native = None
        self._last_warnings = ()
        if not (entity_id := self.linked_helper):
            self._update_status(ScheduleSyncSensor.STATE_OFF)
            return
        self._unsub_state = async_track_state_change_event(
            self.hass, [entity_id], self._on_helper_event
        )
        self._unsub_report = async_track_state_report_event(
            self.hass, [entity_id], self._on_helper_event
        )
        self._update_status(ScheduleSyncSensor.STATE_PENDING)
        self.schedule_callback(self.SYNC_DELAY, self.async_sync)

    def _sync_stop(self):
        """Stops the sync controller: pending callbacks and helper subscriptions."""
        self.cancel_callback(self.async_sync)
        self.cancel_callback(self._async_refresh)
        self._refresh_pending = False
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_report:
            self._unsub_report()
            self._unsub_report = None

    @callback
    def _on_helper_event(self, event: "Event[Any]", /):
        if event.data.get("new_state") is None:
            # keep the subscriptions: the helper might come back (e.g. YAML reload)
            self.cancel_callback(self.async_sync)
            self._last_helper_native = None
            self._update_status(
                ScheduleSyncSensor.STATE_ERROR,
                reason=f"{event.data.get('entity_id', self.linked_helper)} was removed from Home Assistant",
            )
            return
        # debounced: every helper edit (and the helper own state transitions
        # at block boundaries) lands here
        self.schedule_callback(self.SYNC_DELAY, self.async_sync)

    async def async_sync(self, *, force: bool = False) -> str:
        """
        Runs a sync pass: reads the linked helper, compiles it and writes it to the
        device when needed. Returns the resulting sync sensor state
        (ScheduleSyncSensor.STATE_*). When the state is STATE_ERROR, the sensor
        'reason' attribute carries the message. With force=True the compiled
        schedule is written even if the helper didn't change since the last write
        (this also overwrites a drifted device schedule).
        Concurrent calls are serialized.
        """
        # a pending (debounced/retry) run is superseded by this one
        self.cancel_callback(self.async_sync)
        async with self._sync_lock:
            with self.exception_warning("schedule sync"):
                return await self._async_sync(force)
            # unexpected exception (already logged)
            self._last_helper_native = None
            return self._update_status(
                ScheduleSyncSensor.STATE_ERROR,
                reason="unexpected error (see log)",
            )

    async def _async_read_helper(self, entity_id: str, /) -> "Mapping[str, Any]":
        """Returns the weekly schedule of the helper as returned by schedule.get_schedule
        ({"monday": [{"from": time, "to": time, "data": {...}}, ...], ...}).
        Raises HomeAssistantError (or derived) when not available."""
        response = await self.hass.services.async_call(
            SCHEDULE_DOMAIN,
            SCHEDULE_SERVICE_GET,
            {hac.ATTR_ENTITY_ID: entity_id},
            blocking=True,
            return_response=True,
        )
        try:
            return response[entity_id]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise HomeAssistantError(f"no schedule returned for {entity_id}") from error

    async def _async_sync(self, force: bool, /) -> str:
        if not ((entity_id := self.linked_helper) and self.hass_connected):
            return self._update_status(ScheduleSyncSensor.STATE_OFF)

        climate = self.climate
        schedule = climate.schedule
        if not self.parent.is_connected:
            return self._retry("device offline")
        if not schedule.ns_value or (
            (climate.SCHEDULE_NS is mn_h.Appliance_Hub_Mts100_ScheduleB)
            and not schedule._schedule_entry_count_max
        ):
            return self._retry("device schedule not yet known")

        try:
            ha_schedule = await self._async_read_helper(entity_id)
        except HomeAssistantError as error:
            self._last_helper_native = None
            return self._update_status(
                ScheduleSyncSensor.STATE_ERROR,
                reason=f"unable to read {entity_id}: {error}",
            )
        if entity_id != self.linked_helper:
            # the linked helper changed while awaiting: the new one has its own run
            return self.climate.sensor_schedule_sync.native_value  # type: ignore[union-attr]

        try:
            result = compile_schedule(ha_schedule, **schedule_compile_kwargs(climate))
        except ScheduleValidationError as error:
            self._last_helper_native = None
            return self._update_status(
                ScheduleSyncSensor.STATE_ERROR, reason=str(error)
            )

        native = result.native
        warnings = tuple(result.warnings)
        if not force:
            if (self._last_helper_native is not None) and native_equal(
                native, self._last_helper_native
            ):
                # The helper content didn't change since our last sync: never rewrite
                # the device here (drift is flagged, not enforced).
                self._last_warnings = warnings
                if self._refresh_pending:
                    return self._update_status(
                        ScheduleSyncSensor.STATE_PENDING,
                        reason=self.REASON_WAITING,
                        warnings=warnings,
                    )
                return self._check_device_state()
            if (self._last_helper_native is None) and native_equal(
                native, schedule.ns_value
            ):
                # first run (link/restart): the device already carries the helper schedule
                self._last_helper_native = native
                self._last_warnings = warnings
                return self._update_status(
                    ScheduleSyncSensor.STATE_SYNCED, warnings=warnings, last_sync=True
                )

        try:
            response = await schedule.async_request_payload(native)
            if response and (response.method == mc.METHOD_ERROR):
                raise Exception(
                    f"device replied with error {response.payload.get(mc.KEY_ERROR)}"
                )
        except Exception as error:
            self.log(
                self.WARNING,
                "Schedule sync: writing the schedule to the device failed (%s)",
                error,
            )
            self._last_helper_native = None
            return self._update_status(
                ScheduleSyncSensor.STATE_ERROR,
                reason=f"device unreachable or rejected the schedule: {error}",
                warnings=warnings,
            )

        self._last_payload = native
        self._last_helper_native = native
        self._last_warnings = warnings
        self._refresh_pending = True
        self.schedule_callback(self.REFRESH_DELAY, self._async_refresh)
        return self._update_status(
            ScheduleSyncSensor.STATE_PENDING,
            reason=self.REASON_WAITING,
            warnings=warnings,
        )

    def _retry(self, reason: str, /):
        self.schedule_callback(self.RETRY_DELAY, self.async_sync)
        return self._update_status(ScheduleSyncSensor.STATE_PENDING, reason=reason)

    async def _async_refresh(self):
        """Queries the device schedule after a write so that the device state
        (and so the sync status) is confirmed."""
        try:
            await async_refresh_device_schedule(self.climate)
        finally:
            self._refresh_pending = False
        self._check_device_state()

    def _check_device_state(self) -> str:
        """Updates the sync status by comparing the device schedule against the
        last compiled helper schedule. Returns the (resulting) sensor state.
        The sensor is only updated on state transitions."""
        sensor: ScheduleSyncSensor = self.climate.sensor_schedule_sync  # type: ignore[assignment]
        helper_native = self._last_helper_native
        ns_value = self.climate.schedule.ns_value
        if (helper_native is None) or (not ns_value) or self._refresh_pending:
            # nothing compiled yet (or error), device state unknown or waiting confirmation
            return sensor.native_value
        if native_equal(ns_value, helper_native):
            if sensor.native_value != ScheduleSyncSensor.STATE_SYNCED:
                return self._update_status(
                    ScheduleSyncSensor.STATE_SYNCED,
                    warnings=self._last_warnings,
                    last_sync=True,
                )
        elif sensor.native_value != ScheduleSyncSensor.STATE_DRIFT:
            return self._update_status(
                ScheduleSyncSensor.STATE_DRIFT,
                reason=self.REASON_DRIFT,
                warnings=self._last_warnings,
            )
        return sensor.native_value

    def _on_schedule_state(self):
        """Chained to the climate schedule (calendar) flush_state which is also invoked
        on every climate flush: keep it cheap."""
        if not self.hass_connected:
            return
        if (climate := getattr(self, "climate", None)) is None:
            return  # shutting down
        if sensor := climate.sensor_schedule_sync:
            ha_now = dt_util.now()
            sensor.update_device_attributes(
                bool(climate.is_mts_scheduled()),
                ha_now.utcoffset() != ha_now.astimezone(self.parent.tz).utcoffset(),
            )
        ns_value = climate.schedule.ns_value
        if ns_value is self._last_ns_value:
            return
        self._last_ns_value = ns_value
        self._check_device_state()


def schedule_compile_kwargs(climate: "MtsClimate", /) -> "dict[str, Any]":
    """The compile_schedule kwargs carrying the limits of the climate (device) schedule."""
    schedule = climate.schedule
    return {
        "scale": climate.temperature_scale,
        "min_temp": climate.min_temp,
        "max_temp": climate.max_temp,
        "temp_step": climate.target_temperature_step,
        "unit_time": schedule._schedule_unit_time,
        "entry_count_max": schedule._schedule_entry_count_max,
        "entry_count_min": schedule._schedule_entry_count_min,
    }


async def async_refresh_device_schedule(climate: "MtsClimate", /) -> bool:
    """Queries (GET) the device schedule so that the climate schedule entity (and the
    sync status) is updated with the actual device state. Doesn't raise: returns
    False (and logs) when the request fails."""
    schedule = climate.schedule
    try:
        await schedule.handler_ns.async_get(schedule.index)
        return True
    except Exception as error:
        climate.log(
            climate.WARNING,
            "Schedule sync: unable to query the device schedule (%s)",
            error,
        )
        return False
