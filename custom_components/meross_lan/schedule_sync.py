"""
Entities bridging a HomeAssistant Schedule helper (schedule.*) to the on-device
weekly schedule of Meross thermostats (MtsClimate).
- ScheduleHelperSelect: selects the helper linked to the thermostat and (will) run
  the one-way sync controller (helper -> device).
- ScheduleSyncSensor: diagnostic status of the sync.
These are aliased as MtsClimate.ScheduleHelperSelect / MtsClimate.ScheduleSyncSensor
(see climate.py) so that derived climate classes can override them.
"""

from typing import TYPE_CHECKING, override

from homeassistant.core import CoreState, callback
from homeassistant.util import dt as dt_util

from .const import hac
from .select import SelectEntity
from .sensor import EnumSensorEntity

if TYPE_CHECKING:
    from typing import Any, Callable, Final, Iterable

    from .climate import MtsClimate


SCHEDULE_DOMAIN = "schedule"
"""HA core Schedule helper entity domain."""


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

        climate: "MtsClimate"
        _unsub_started: Callable[[], None] | None
        _unsub_state: Callable[[], None] | None
        _unsub_report: Callable[[], None] | None
        _last_payload: dict[str, Any] | None
        _last_ns_value: Any
        _sync_running: bool
        _sync_again: bool

    _INIT_entity_key = "schedule_helper"
    SYNC_DELAY = 2
    REFRESH_DELAY = 3
    RETRY_DELAY = 60

    # HA core entity attributes:
    _attr_available = True
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:calendar-sync"

    _INIT__sync_running = False
    _INIT__sync_again = False
    _AUTO_INIT = (
        "_unsub_started",
        "_unsub_state",
        "_unsub_report",
        "_last_payload",
        "_last_ns_value",
        "_sync_running",
        "_sync_again",
    )
    __slots__ = ("climate",)

    def __init__(self, climate: "MtsClimate", /):
        self.climate = climate
        super().__init__(climate)

    @override
    async def async_shutdown(self):
        self._cancel_started_listener()
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

    @callback
    def _setup_options(self, *_):
        self._unsub_started = None  # one-shot listener (if any) already fired
        self.options = [
            hac.STATE_OFF,
            *sorted(self.hass.states.async_entity_ids(SCHEDULE_DOMAIN)),
        ]
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

    def _cancel_started_listener(self):
        if self._unsub_started:
            self._unsub_started()
            self._unsub_started = None

    def _update_status(self, state: str, /, **kwargs: "Any"):
        """Forwards a status update to the sync sensor adding the linked helper."""
        kwargs.setdefault("helper", self.linked_helper)
        self.climate.sensor_schedule_sync.update_status(state, **kwargs)  # type: ignore[union-attr]

    def _sync_start(self):
        # TODO(step 3): sync controller (subscribe helper state events, schedule _async_sync)
        self._sync_stop()
        if self.linked_helper:
            self._update_status(ScheduleSyncSensor.STATE_PENDING)
        else:
            self._last_payload = None
            self._update_status(ScheduleSyncSensor.STATE_OFF)

    def _sync_stop(self):
        # TODO(step 3): sync controller (cancel pending callbacks and helper subscriptions)
        pass
