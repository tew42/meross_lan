"""
Entity services exposing the on-device weekly schedule of Meross thermostats
(MtsClimate) in the HomeAssistant Schedule helper format:
- meross_lan.get_schedule: returns the device schedule (helper format + raw payload + limits)
- meross_lan.set_schedule: compiles a helper-formatted schedule and writes it to the device
- meross_lan.sync_schedule: (re)syncs the linked Schedule helper (see schedule_sync.py)

The services are registered on the meross_lan 'climate' EntityPlatform (see climate.py)
so they target climate entities of this integration.
"""

from typing import TYPE_CHECKING

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

try:
    # HA core 2026.9 introduced probatio in place of voluptuous
    import probatio as vol
except:
    import voluptuous as vol

from . import const as mlc
from .helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    KEY_DATA,
    KEY_FROM,
    KEY_TO,
    MTS_SCHEDULE_WEEKDAY,
    ScheduleValidationError,
    compile_schedule,
    decompile_schedule,
)
from .merossclient.protocol import const as mc
from .schedule_sync import (
    ScheduleSyncSensor,
    async_refresh_device_schedule,
    schedule_compile_kwargs,
)

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import ServiceCall, ServiceResponse
    from homeassistant.helpers.entity_platform import EntityPlatform

    from .climate import MtsClimate


ATTR_SCHEDULE = "schedule"

_BLOCK_SCHEMA = {
    vol.Required(KEY_FROM): vol.Any(cv.time, cv.string),
    vol.Required(KEY_TO): vol.Any(cv.time, cv.string),
    vol.Optional(KEY_DATA): dict,
}
SET_SCHEDULE_SCHEMA = {
    vol.Required(ATTR_SCHEDULE): {
        vol.Optional(day): [_BLOCK_SCHEMA] for day in HA_SCHEDULE_WEEKDAY
    }
}


def _check_supported(entity: "MtsClimate", /):
    if not (
        getattr(entity, "SCHEDULE_SYNC_SUPPORTED", False)
        and getattr(entity, "schedule", None)
    ):
        raise ServiceValidationError(
            "Schedule sync is not supported on this thermostat"
        )


async def async_service_get_schedule(
    entity: "MtsClimate", call: "ServiceCall", /
) -> "ServiceResponse":
    _check_supported(entity)
    schedule = entity.schedule
    ns_value = schedule.ns_value
    if not ns_value:
        raise HomeAssistantError("Schedule not available yet")
    try:
        ha_schedule = decompile_schedule(ns_value, scale=entity.temperature_scale)
    except ScheduleValidationError as e:
        raise ServiceValidationError(str(e)) from e
    select = entity.select_schedule_helper
    return {
        "schedule": ha_schedule,  # type: ignore[dict-item]
        "native": {
            day: [list(entry) for entry in ns_value[day]]
            for day in MTS_SCHEDULE_WEEKDAY
            if day in ns_value
        },
        "limits": {
            "unit_time": schedule._schedule_unit_time,
            "entry_count_max": schedule._schedule_entry_count_max,
            "entry_count_min": schedule._schedule_entry_count_min,
            "temperature_scale": entity.temperature_scale,
            "min_temp": entity.min_temp,
            "max_temp": entity.max_temp,
            "temp_step": entity.target_temperature_step,
        },
        "schedule_mode_active": bool(entity.is_mts_scheduled()),
        "linked_helper": select.linked_helper if select else None,
    }


async def async_service_set_schedule(entity: "MtsClimate", call: "ServiceCall", /):
    _check_supported(entity)
    schedule = entity.schedule
    if not schedule.ns_value:
        # device limits (entry count) might be unknown until the schedule is received
        raise HomeAssistantError("Schedule not available yet")
    try:
        result = compile_schedule(
            call.data[ATTR_SCHEDULE], **schedule_compile_kwargs(entity)
        )
    except ScheduleValidationError as e:
        raise ServiceValidationError(str(e)) from e
    for warning in result.warnings:
        entity.log(entity.WARNING, "set_schedule: %s", warning)
    try:
        response = await schedule.async_request_payload(result.native)
    except Exception as e:
        raise HomeAssistantError(f"Device write failed: {e}") from e
    if response and (response.method == mc.METHOD_ERROR):
        raise HomeAssistantError(
            f"Device write failed: {response.payload.get(mc.KEY_ERROR)}"
        )
    if select := entity.select_schedule_helper:
        # a linked helper will then report 'drift' (if different)
        select._last_payload = result.native
    await async_refresh_device_schedule(entity)


async def async_service_sync_schedule(entity: "MtsClimate", call: "ServiceCall", /):
    _check_supported(entity)
    select = entity.select_schedule_helper
    if not (select and select.linked_helper):
        raise ServiceValidationError("No schedule helper linked to this thermostat")
    state = await select.async_sync(force=True)
    if state == ScheduleSyncSensor.STATE_ERROR:
        sensor = entity.sensor_schedule_sync
        raise HomeAssistantError(
            (
                sensor.extra_state_attributes.get(ScheduleSyncSensor.ATTR_REASON)
                if sensor
                else None
            )
            or "Schedule sync failed"
        )


def async_register(platform: "EntityPlatform", /):
    """Registers the schedule entity services on the meross_lan climate platform.
    This is idempotent (HA skips already registered services) so it can be
    called on every config entry setup."""
    platform.async_register_entity_service(
        mlc.SERVICE_GET_SCHEDULE,
        None,
        async_service_get_schedule,
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        mlc.SERVICE_SET_SCHEDULE,
        SET_SCHEDULE_SCHEMA,
        async_service_set_schedule,
    )
    platform.async_register_entity_service(
        mlc.SERVICE_SYNC_SCHEDULE,
        None,
        async_service_sync_schedule,
    )
