from __future__ import annotations

import random
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, ServiceCall
from homeassistant.const import CONF_MAC, EVENT_HOMEASSISTANT_STOP, ATTR_ENTITY_ID
from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    CONF_RESET,
    CONF_DELAY,
    CONF_MODEL,
    CONF_BRIGHTNESS_MODE,
    DEFAULT_RESET,
    DEFAULT_BRIGHTNESS_MODE,
    WEEK_DAYS,
)
from .elkbledom import BLEDOMInstance
from .model import ensure_models_loaded
import logging

LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

# Service names
SERVICE_SET_RANDOM_COLOR = "set_random_color"
SERVICE_SET_RGB_COLOR = "set_rgb_color"
SERVICE_SYNC_TIME = "sync_time"
SERVICE_SCHEDULE_ON = "schedule_on"
SERVICE_SCHEDULE_OFF = "schedule_off"

# Valid weekday selectors for the schedule services (every WEEK_DAYS member
# except the "none" sentinel, including the "all"/"week_days"/"weekend_days" aggregates).
SCHEDULE_DAY_NAMES = [d.name for d in WEEK_DAYS if d.name != "none"]

# Service schemas
SERVICE_SET_RANDOM_COLOR_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional("brightness", default=255): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
})

SERVICE_SET_RGB_COLOR_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Required("r"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Required("g"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Required("b"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Optional("brightness", default=255): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
})

SERVICE_SYNC_TIME_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
})

SERVICE_SCHEDULE_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Required("hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Required("minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Optional("enabled", default=True): cv.boolean,
    vol.Optional("days", default=["all"]): vol.All(cv.ensure_list, [vol.In(SCHEDULE_DAY_NAMES)]),
})


def _entry_option(entry: ConfigEntry, key, default=None):
    """Read a setting preferring options, then data, then a default.

    Uses an explicit None check (not ``or``) so falsy-but-valid values such as
    delay=0 ("stay connected") are honored instead of falling through.
    """
    val = entry.options.get(key)
    if val is None:
        val = entry.data.get(key, default)
    return val


def _instances_for_entities(hass: HomeAssistant, entity_ids):
    """Resolve target entity_ids to their BLEDOMInstance objects (deduplicated)."""
    ent_reg = er.async_get(hass)
    instances = {}
    for entity_id in entity_ids:
        entity_entry = ent_reg.async_get(entity_id)
        if entity_entry and entity_entry.config_entry_id:
            instance = hass.data.get(DOMAIN, {}).get(entity_entry.config_entry_id)
            if instance is not None:
                instances[entity_entry.config_entry_id] = instance
    return list(instances.values())


def _days_to_mask(day_names) -> int:
    """OR a list of WEEK_DAYS member names into a single bitmask."""
    mask = 0
    for name in day_names:
        mask |= WEEK_DAYS[name].value
    return mask

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ElkBLEDOM from a config entry."""
    reset = _entry_option(entry, CONF_RESET, DEFAULT_RESET)
    delay = _entry_option(entry, CONF_DELAY)  # None => keep the connection alive
    mac = _entry_option(entry, CONF_MAC)
    forced_model = _entry_option(entry, CONF_MODEL)
    brightness_mode = _entry_option(entry, CONF_BRIGHTNESS_MODE, DEFAULT_BRIGHTNESS_MODE)
    LOGGER.debug("Config: Reset: %s, Delay: %s, Mac: %s, Forced Model: %s, Brightness mode: %s", reset, delay, mac, forced_model, brightness_mode)

    # Ensure models are loaded (will reuse if already in hass.data)
    await ensure_models_loaded(hass)

    instance = BLEDOMInstance(entry.data[CONF_MAC], reset, delay, hass, forced_model, brightness_mode, entry.data.get("name"))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = instance
   
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_stop(event: Event) -> None:
        """Close the connection."""
        await instance.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    
    # Register services (only once for all entries)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_RANDOM_COLOR):
        async def handle_set_random_color(call: ServiceCall) -> None:
            """Handle the set_random_color service call."""
            entity_ids = call.data[ATTR_ENTITY_ID]
            brightness = call.data.get("brightness", 255)
            
            # Generate random RGB values
            r = random.randint(0, 255)
            g = random.randint(0, 255)
            b = random.randint(0, 255)
            
            # Call light.turn_on with random color for each entity
            await hass.services.async_call(
                "light",
                "turn_on",
                {
                    ATTR_ENTITY_ID: entity_ids,
                    "rgb_color": [r, g, b],
                    "brightness": brightness,
                },
                blocking=True,
            )
            LOGGER.debug(
                "Random color set to RGB(%d, %d, %d) with brightness %d for entities: %s",
                r, g, b, brightness, entity_ids
            )
        
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_RANDOM_COLOR,
            handle_set_random_color,
            schema=SERVICE_SET_RANDOM_COLOR_SCHEMA,
        )
    
    if not hass.services.has_service(DOMAIN, SERVICE_SET_RGB_COLOR):
        async def handle_set_rgb_color(call: ServiceCall) -> None:
            """Handle the set_rgb_color service call."""
            entity_ids = call.data[ATTR_ENTITY_ID]
            r = call.data["r"]
            g = call.data["g"]
            b = call.data["b"]
            brightness = call.data.get("brightness", 255)
            
            # Call light.turn_on with specified RGB color
            await hass.services.async_call(
                "light",
                "turn_on",
                {
                    ATTR_ENTITY_ID: entity_ids,
                    "rgb_color": [r, g, b],
                    "brightness": brightness,
                },
                blocking=True,
            )
            LOGGER.debug(
                "RGB color set to (%d, %d, %d) with brightness %d for entities: %s",
                r, g, b, brightness, entity_ids
            )
        
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_RGB_COLOR,
            handle_set_rgb_color,
            schema=SERVICE_SET_RGB_COLOR_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_TIME):
        async def handle_sync_time(call: ServiceCall) -> None:
            """Sync the device clock to Home Assistant's current time."""
            for instance in _instances_for_entities(hass, call.data[ATTR_ENTITY_ID]):
                try:
                    await instance.sync_time()
                except Exception as err:
                    LOGGER.error("sync_time failed for %s: %s", instance.address, err)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_TIME,
            handle_sync_time,
            schema=SERVICE_SYNC_TIME_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SCHEDULE_ON):
        async def handle_schedule_on(call: ServiceCall) -> None:
            """Program the device's daily turn-on timer."""
            mask = _days_to_mask(call.data["days"])
            for instance in _instances_for_entities(hass, call.data[ATTR_ENTITY_ID]):
                try:
                    await instance.set_scheduler_on(mask, call.data["hour"], call.data["minute"], call.data["enabled"])
                except Exception as err:
                    LOGGER.error("schedule_on failed for %s: %s", instance.address, err)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SCHEDULE_ON,
            handle_schedule_on,
            schema=SERVICE_SCHEDULE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SCHEDULE_OFF):
        async def handle_schedule_off(call: ServiceCall) -> None:
            """Program the device's daily turn-off timer."""
            mask = _days_to_mask(call.data["days"])
            for instance in _instances_for_entities(hass, call.data[ATTR_ENTITY_ID]):
                try:
                    await instance.set_scheduler_off(mask, call.data["hour"], call.data["minute"], call.data["enabled"])
                except Exception as err:
                    LOGGER.error("schedule_off failed for %s: %s", instance.address, err)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SCHEDULE_OFF,
            handle_schedule_off,
            schema=SERVICE_SCHEDULE_SCHEMA,
        )

    return True
   
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        instance = hass.data[DOMAIN].pop(entry.entry_id, None)
        if instance is not None:
            await instance.stop()
    return unload_ok

async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update.

    Brightness mode is applied live (no reload, so it isn't reset to "auto").
    A reload is triggered only when an option consumed at setup time (reset,
    delay, forced model) actually changed.
    """
    instance = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if instance is None:
        return

    brightness_mode = entry.options.get(CONF_BRIGHTNESS_MODE)
    if brightness_mode:
        await instance.apply_brightness_mode(brightness_mode)

    new_reset = _entry_option(entry, CONF_RESET, DEFAULT_RESET)
    new_delay = _entry_option(entry, CONF_DELAY)
    new_model = _entry_option(entry, CONF_MODEL)
    if (
        bool(new_reset) != bool(instance.reset)
        or new_delay != instance.delay
        or new_model != instance.forced_model
    ):
        LOGGER.debug("Reloading %s due to changed setup options", entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
