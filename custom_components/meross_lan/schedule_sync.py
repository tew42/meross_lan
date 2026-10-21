"""
Entities bridging a HomeAssistant Schedule helper (schedule.*) to the on-device
weekly schedule of Meross thermostats (MtsClimate).
- ScheduleHelperSelect: selects the helper linked to the thermostat and runs
  the one-way sync controller (helper -> device): the helper is compiled and
  written to the device when it is selected and whenever its content changes.
  A device schedule changed elsewhere is flagged as 'drift' (not enforced) and
  restoring the link (HA restart, entry reload) never writes.
- ScheduleSyncSensor: diagnostic status of the sync.
These are aliased as MtsClimate.ScheduleHelperSelect / MtsClimate.ScheduleSyncSensor
(see climate.py) so that derived climate classes can override them.
"""

import asyncio
from typing import TYPE_CHECKING, override

from homeassistant.components.schedule.const import (
    DOMAIN as SCHEDULE_DOMAIN,
    SERVICE_GET as SCHEDULE_SERVICE_GET,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_added_domain,
    async_track_state_change_event,
    async_track_state_removed_domain,
    async_track_state_report_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import hac
from .helpers.schedule import ScheduleValidationError, compile_schedule, native_equal
from .merossclient.protocol import const as mc
from .select import SelectEntity
from .sensor import EnumSensorEntity

if TYPE_CHECKING:
    from typing import Any, Callable, Iterable, Mapping

    from homeassistant.core import Event

    from .climate import MtsClimate


class ScheduleSyncSensor(EnumSensorEntity):
    """Diagnostic sensor reporting the state of the schedule helper -> device sync."""

    if TYPE_CHECKING:
        native_value: str

    _INIT_entity_key = "schedule_sync"

    STATE_OFF = "off"
    """No helper linked."""
    STATE_PENDING = "pending"
    """Sync scheduled/in progress or waiting for the device (see 'reason')."""
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


class ScheduleHelperSelect(SelectEntity.RestoreEntity, SelectEntity):
    """
    A select entity listing the HA Schedule helpers (schedule.*) so that the user
    can link one to the thermostat. The linked helper is compiled into the device
    weekly schedule payload and written to the device whenever it changes (one-way).
    """

    if TYPE_CHECKING:
        climate: "MtsClimate"
        _sync_lock: asyncio.Lock
        """Serializes sync runs and set_schedule writes."""
        _unsubs: list[Callable[[], None]]
        """Entity lifetime subscriptions (HA started, helpers added/removed)."""
        _helper_unsubs: list[Callable[[], None]]
        """Linked helper state subscriptions."""
        _sync_gen: int
        """Bumped whenever the link is stopped/restarted: a sync run checks it after
        every await and bails out when it changed (superseded run)."""
        _last_helper_native: dict[str, Any] | None
        """The compiled helper last written (and confirmed) or adopted: the reference
        for the drift check. None when not (yet) synced or in error."""
        _last_warnings: tuple[str, ...]
        """Compile warnings of _last_helper_native."""
        _adopt: bool
        """Set when the link is restored (setup/restart): the next compiled helper is
        adopted as the reference without being written to the device."""
        _retry_pending: bool
        """A run was deferred (device offline/schedule unknown/write unconfirmed)."""
        _force_pending: bool
        """The deferred run is a forced one (sync_schedule)."""

    _INIT_entity_key = "schedule_helper"
    SYNC_DELAY = 2
    """Debounce delay before syncing after a helper change/(re)selection."""
    RETRY_DELAY = 60
    """Retry delay when the device is offline or its schedule is unknown/unconfirmed."""
    REASON_OFFLINE = "device offline"
    REASON_UNKNOWN = "device schedule not yet known"
    REASON_WRITING = "writing the schedule to the device"
    REASON_CONFIRM = "waiting for device confirmation"
    REASON_DRIFT = "device schedule differs from the linked helper"

    # HA core entity attributes:
    _attr_available = True
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:calendar-sync"

    _INIT__sync_gen = 0
    _INIT__last_warnings = ()
    _INIT__adopt = False
    _INIT__retry_pending = False
    _INIT__force_pending = False
    _AUTO_INIT = (
        "_sync_gen",
        "_last_helper_native",
        "_last_warnings",
        "_adopt",
        "_retry_pending",
        "_force_pending",
    )
    _AUTO_DESTROY = ("climate",)
    __slots__ = (
        "_sync_lock",
        "_unsubs",
        "_helper_unsubs",
    )

    def __init__(self, climate: "MtsClimate", /):
        self.climate = climate
        self._sync_lock = asyncio.Lock()
        self._unsubs = []
        self._helper_unsubs = []
        super().__init__(climate)
        # Watch the device schedule state (the calendar flush_state is also
        # invoked on every climate flush). This can't be unregistered and is
        # cleaned up on the calendar shutdown: _on_schedule_state guards itself.
        climate.schedule.register_state_callback(self._on_schedule_state)

    @override
    async def async_shutdown(self):
        self._stop()
        await super().async_shutdown()

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
        # _setup_options runs now (when HA is running) or when HA has started.
        # Call super after since _setup_options flushes the state (it should
        # only when called by the hass bus)
        self._unsubs.append(async_at_started(self.hass, self._setup_options))
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self._stop()
        await super().async_will_remove_from_hass()

    # interface: SelectEntity
    @override
    async def async_select_option(self, option: str):
        self.update_option(option)
        self._sync_start(True)

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

    async def async_sync(self, force: bool = False) -> str:
        """
        Runs a sync pass: reads the linked helper, compiles it and writes it to the
        device when needed. Returns the resulting sync sensor state
        (ScheduleSyncSensor.STATE_*; the sensor 'reason' attribute carries the
        details). With force=True the compiled schedule is written even if the
        helper didn't change since the last write (this also overwrites a drifted
        device schedule). Concurrent calls are serialized.
        """
        # a pending (debounced/retry) run is superseded by this one
        self.cancel_callback(self.async_sync)
        async with self._sync_lock:
            gen = self._sync_gen
            compile_error = False
            try:
                return await self._async_sync(gen, force or self._force_pending)
            except ScheduleValidationError as error:
                reason = str(error)
                compile_error = True
            except HomeAssistantError as error:
                reason = str(error)
            except Exception as error:
                # programming error: logged, the status only points to the log
                self.log_exception(self.WARNING, error, "schedule sync")
                reason = "unexpected error (see log)"
            if self._stale(gen):
                return self._sensor_state()
            if compile_error:
                # The helper content was read: a later successful compile means the
                # user edited it, which must be written (no adoption then).
                # A read failure (transient) keeps the adoption pending instead.
                self._adopt = False
            self.log(self.WARNING, "Schedule sync failed: %s", reason, timeout=14400)
            self._last_helper_native = None
            return self._update_status(ScheduleSyncSensor.STATE_ERROR, reason=reason)

    async def async_write(self, native: "Mapping[str, Any]", /) -> bool:
        """Writes a compiled schedule to the device (set_schedule service) serialized
        with the sync runs. The sync reference is left alone so that a linked helper
        which differs reports drift. See async_write_schedule for the result."""
        async with self._sync_lock:
            confirmed = await async_write_schedule(self.climate, native)
            self._check_device_state()
            return confirmed

    async def _async_read_helper(self, entity_id: str, /) -> "Mapping[str, Any]":
        """Returns the weekly schedule of the helper as returned by schedule.get_schedule
        ({"monday": [{"from": time, "to": time, "data": {...}}, ...], ...}).
        Raises HomeAssistantError when not available."""
        try:
            response = await self.hass.services.async_call(
                SCHEDULE_DOMAIN,
                SCHEDULE_SERVICE_GET,
                {hac.ATTR_ENTITY_ID: entity_id},
                blocking=True,
                return_response=True,
            )
            return response[entity_id]  # type: ignore[index]
        except (HomeAssistantError, KeyError, TypeError) as error:
            raise HomeAssistantError(f"unable to read {entity_id}: {error}") from error

    async def _async_sync(self, gen: int, force: bool, /) -> str:
        if self._stale(gen):
            return self._sensor_state()
        if not (entity_id := self.linked_helper):
            return self._update_status(ScheduleSyncSensor.STATE_OFF)
        climate = self.climate
        if not self.parent.is_connected:
            return self._retry(self.REASON_OFFLINE, force)
        if not climate.schedule_limits_known():
            return self._retry(self.REASON_UNKNOWN, force)
        self._retry_pending = self._force_pending = False

        ha_schedule = await self._async_read_helper(entity_id)
        if self._stale(gen):
            return self._sensor_state()
        result = compile_schedule(ha_schedule, **schedule_compile_kwargs(climate))
        native = result.native
        warnings = tuple(result.warnings)

        if not force and (
            self._adopt
            or native_equal(
                native, self._last_helper_native or climate.schedule.ns_value
            )
        ):
            # The link is being restored (restart/reload: never write then), the
            # helper didn't change since the last sync or the device already
            # carries it: the compiled helper becomes the reference and a
            # different device schedule is only flagged (drift is not enforced).
            self._adopt = False
            self._last_helper_native = native
            self._last_warnings = warnings
            return self._check_device_state()

        self._adopt = False
        self._last_helper_native = None  # no drift checks while writing
        self._update_status(
            ScheduleSyncSensor.STATE_PENDING,
            reason=self.REASON_WRITING,
            warnings=warnings,
        )
        confirmed = await async_write_schedule(climate, native)
        if self._stale(gen):
            return self._sensor_state()
        if not confirmed:
            # the retry writes again unless the device reports the schedule meanwhile
            return self._retry(self.REASON_CONFIRM)
        self._last_helper_native = native
        self._last_warnings = warnings
        return self._check_device_state()

    def _stale(self, gen: int, /) -> bool:
        """True when the sync run started at 'gen' was superseded (link stopped or
        changed) or the entity was removed: the run must not touch any state."""
        return gen != self._sync_gen or not self.hass_connected

    def _retry(self, reason: str, force: bool = False, /) -> str:
        self._retry_pending = True
        self._force_pending = force
        self.schedule_callback(self.RETRY_DELAY, self.async_sync)
        return self._update_status(ScheduleSyncSensor.STATE_PENDING, reason=reason)

    def _check_device_state(self) -> str:
        """Updates the sync status by comparing the device schedule against the
        last compiled helper schedule. Returns the (resulting) sensor state.
        The sensor is only updated on state transitions."""
        state = self._sensor_state()
        reference = self._last_helper_native
        ns_value = self.climate.schedule.ns_value
        if not (self.linked_helper and reference and ns_value):
            # not linked, nothing compiled yet (or error) or device state unknown
            return state
        if native_equal(ns_value, reference):
            if state != ScheduleSyncSensor.STATE_SYNCED:
                return self._update_status(
                    ScheduleSyncSensor.STATE_SYNCED,
                    warnings=self._last_warnings,
                    last_sync=True,
                )
        elif state != ScheduleSyncSensor.STATE_DRIFT:
            return self._update_status(
                ScheduleSyncSensor.STATE_DRIFT,
                reason=self.REASON_DRIFT,
                warnings=self._last_warnings,
            )
        return state

    def _on_schedule_state(self):
        """Chained to the climate schedule (calendar) flush_state which is also invoked
        on every climate flush: keep it cheap."""
        if not (self.hass_connected and self.linked_helper):
            return
        if self._retry_pending:
            # the device (schedule) is likely available now: don't wait the retry timer
            self.schedule_callback(0, self.async_sync)
        else:
            self._check_device_state()

    # link management
    def _build_options(self):
        return [
            hac.STATE_OFF,
            *sorted(self.hass.states.async_entity_ids(SCHEDULE_DOMAIN)),
        ]

    @callback
    def _setup_options(self, *_):
        hass = self.hass
        # keep the options updated when schedule helpers are added/removed
        self._unsubs.append(
            async_track_state_added_domain(
                hass, SCHEDULE_DOMAIN, self._on_helpers_changed
            )
        )
        self._unsubs.append(
            async_track_state_removed_domain(
                hass, SCHEDULE_DOMAIN, self._on_helpers_changed
            )
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
        # restoring the link: don't write (see _adopt)
        self._sync_start(False)

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

    def _update_status(self, state: str, /, **kwargs: "Any"):
        """Forwards a status update to the sync sensor adding the linked helper.
        Returns the state for convenience."""
        kwargs.setdefault("helper", self.linked_helper)
        self.climate.sensor_schedule_sync.update_status(state, **kwargs)  # type: ignore[union-attr]
        return state

    def _sensor_state(self) -> str:
        """The current sync state (unchanged)."""
        try:
            return self.climate.sensor_schedule_sync.native_value  # type: ignore[union-attr]
        except AttributeError:  # shut down
            return ScheduleSyncSensor.STATE_OFF

    def _sync_start(self, write: bool, /):
        """(Re)starts the sync controller for the currently linked helper (if any).
        With write=False (link restored at setup) the compiled helper is adopted as
        the reference without writing it: a different device schedule is flagged."""
        self._sync_stop()
        self._last_helper_native = None
        self._last_warnings = ()
        self._adopt = not write
        if not (entity_id := self.linked_helper):
            self._update_status(ScheduleSyncSensor.STATE_OFF)
            return
        self._helper_unsubs = [
            async_track_state_change_event(self.hass, entity_id, self._on_helper_event),
            async_track_state_report_event(self.hass, entity_id, self._on_helper_event),
        ]
        self._update_status(ScheduleSyncSensor.STATE_PENDING)
        self.schedule_callback(self.SYNC_DELAY, self.async_sync)

    def _sync_stop(self):
        """Stops the sync controller: pending runs and helper subscriptions."""
        self._sync_gen += 1
        self._retry_pending = self._force_pending = False
        self.cancel_callback(self.async_sync)
        for unsub in self._helper_unsubs:
            unsub()
        self._helper_unsubs = []

    def _stop(self):
        """Stops everything (entity removal/shutdown)."""
        self._sync_stop()
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    @callback
    def _on_helper_event(self, event: "Event[Any]", /):
        if event.data["new_state"] is None:
            # keep the subscriptions: the helper might come back (e.g. YAML reload)
            self.cancel_callback(self.async_sync)
            self._retry_pending = self._force_pending = False
            self._last_helper_native = None
            self._update_status(
                ScheduleSyncSensor.STATE_ERROR,
                reason=f"{self.linked_helper} was removed from Home Assistant",
            )
            return
        # debounced: every helper edit (and the helper own state transitions
        # at block boundaries) lands here
        self.schedule_callback(self.SYNC_DELAY, self.async_sync)


def schedule_compile_kwargs(climate: "MtsClimate", /) -> "dict[str, Any]":
    """The compile_schedule kwargs carrying the limits of the climate (device) schedule."""
    return {
        "scale": climate.temperature_scale,
        "min_temp": climate.min_temp,
        "max_temp": climate.max_temp,
        "temp_step": climate.target_temperature_step,
        **climate.schedule.limits,
    }


async def async_write_schedule(
    climate: "MtsClimate", native: "Mapping[str, Any]", /
) -> bool:
    """Writes a compiled (native) schedule to the device and queries it back (GET)
    so that the climate schedule entity (and the sync status) carries the actual
    device state. Raises HomeAssistantError when the write fails or is rejected.
    Returns False when only the confirmation query failed."""
    schedule = climate.schedule
    try:
        response = await schedule.async_request_payload(dict(native))
    except Exception as error:
        raise HomeAssistantError(f"device write failed: {error}") from error
    if response and (response.method == mc.METHOD_ERROR):
        raise HomeAssistantError(
            f"device rejected the schedule: {response.payload.get(mc.KEY_ERROR)}"
        )
    # No delay before querying: the device (or the hub for its valves) replies
    # with the schedule it stores. Should a device still report the old one, the
    # status shows drift until its next update (poll or push) fixes it.
    try:
        await schedule.handler_ns.async_get(schedule.index)
    except Exception as error:
        climate.log(
            climate.WARNING,
            "Unable to query the device schedule after writing it (%s)",
            error,
        )
        return False
    return True
