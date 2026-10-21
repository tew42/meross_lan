"""
Entity services exposing the on-device weekly schedule of Meross thermostats
(MtsClimate) in the HomeAssistant Schedule helper format:
- meross_lan.get_schedule: returns the device schedule (helper format) and its limits
- meross_lan.set_schedule: compiles a helper-formatted schedule and writes it to the device
- meross_lan.sync_schedule: (re)syncs the linked Schedule helper now (see schedule_sync.py)

They target the climate entities of this integration and are registered (once)
by ComponentApi.
"""

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_platform_entity_service

try:
    # HA core 2026.9 introduced probatio in place of voluptuous
    import probatio as vol
except ImportError:
    import voluptuous as vol

from . import const as mlc
from .helpers.schedule import (
    HA_SCHEDULE_WEEKDAY,
    KEY_DATA,
    KEY_FROM,
    KEY_TO,
    ScheduleValidationError,
    compile_schedule,
    decompile_schedule,
)
from .schedule_sync import ScheduleSyncSensor, schedule_compile_kwargs

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from .climate import MtsClimate
    from .schedule_sync import ScheduleHelperSelect


ATTR_SCHEDULE = "schedule"

_BLOCK_SCHEMA = {
    vol.Required(KEY_FROM): cv.string,
    vol.Required(KEY_TO): cv.string,
    vol.Optional(KEY_DATA): dict,
}
SET_SCHEDULE_SCHEMA = {
    vol.Required(ATTR_SCHEDULE): {
        vol.Required(day): [_BLOCK_SCHEMA] for day in HA_SCHEDULE_WEEKDAY
    }
}


def _get_select(entity: "MtsClimate", /) -> "ScheduleHelperSelect":
    if (select := entity.select_schedule_helper) is None:
        raise ServiceValidationError(
            "Schedule sync is not supported on this thermostat"
        )
    return select


async def async_service_get_schedule(
    entity: "MtsClimate", call: "ServiceCall", /
) -> "ServiceResponse":
    select = _get_select(entity)
    schedule = entity.schedule
    if not (ns_value := schedule.ns_value):
        raise HomeAssistantError("Schedule not available yet")
    try:
        ha_schedule = decompile_schedule(ns_value, scale=entity.temperature_scale)
    except ScheduleValidationError as error:
        # device data, not user input
        raise HomeAssistantError(f"Unsupported device schedule: {error}") from error
    return {
        "schedule": ha_schedule,  # type: ignore[dict-item]
        "limits": schedule.limits
        | {
            "temperature_scale": entity.temperature_scale,
            "min_temp": entity.min_temp,
            "max_temp": entity.max_temp,
            "temp_step": entity.target_temperature_step,
        },
        "linked_helper": select.linked_helper,
    }


async def async_service_set_schedule(entity: "MtsClimate", call: "ServiceCall", /):
    select = _get_select(entity)
    if not entity.schedule_limits_known():
        raise HomeAssistantError("Schedule not available yet")
    try:
        result = compile_schedule(
            call.data[ATTR_SCHEDULE], **schedule_compile_kwargs(entity)
        )
    except ScheduleValidationError as error:
        raise ServiceValidationError(str(error)) from error
    for warning in result.warnings:
        entity.log(entity.WARNING, "set_schedule: %s", warning)
    await select.async_write(result.native)


async def async_service_sync_schedule(entity: "MtsClimate", call: "ServiceCall", /):
    select = _get_select(entity)
    if not select.linked_helper:
        raise ServiceValidationError("No schedule helper linked to this thermostat")
    state = await select.async_sync(force=True)
    reason = entity.sensor_schedule_sync.extra_state_attributes.get(  # type: ignore[union-attr]
        ScheduleSyncSensor.ATTR_REASON
    )
    if state == ScheduleSyncSensor.STATE_ERROR:
        raise HomeAssistantError(reason or "Schedule sync failed")
    if (state == ScheduleSyncSensor.STATE_PENDING) and (
        reason != select.REASON_CONFIRM
    ):
        # nothing was written (device offline or its schedule not yet known)
        raise HomeAssistantError(f"{reason}, sync will be retried")


SERVICES = (
    (
        mlc.SERVICE_GET_SCHEDULE,
        async_service_get_schedule,
        None,
        SupportsResponse.ONLY,
    ),
    (
        mlc.SERVICE_SET_SCHEDULE,
        async_service_set_schedule,
        SET_SCHEDULE_SCHEMA,
        SupportsResponse.NONE,
    ),
    (
        mlc.SERVICE_SYNC_SCHEDULE,
        async_service_sync_schedule,
        None,
        SupportsResponse.NONE,
    ),
)


def async_register(hass: "HomeAssistant", /):
    """Registers the schedule services targeting the meross_lan climate entities."""
    for name, func, schema, supports_response in SERVICES:
        async_register_platform_entity_service(
            hass,
            mlc.DOMAIN,
            name,
            entity_domain=Platform.CLIMATE,
            func=func,
            schema=schema,
            supports_response=supports_response,
        )


def async_unregister(hass: "HomeAssistant", /):
    for name, *_ in SERVICES:
        hass.services.async_remove(mlc.DOMAIN, name)
