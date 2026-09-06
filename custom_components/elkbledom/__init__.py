from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import voluptuous as vol
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothReachabilityIntent,
    BluetoothScanningMode,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, CONF_MAC, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .address import addresses_equal, normalize_address, normalize_entity_unique_id
from .const import (
    AUTOMATIC_MODEL,
    CONF_BRIGHTNESS_MODE,
    CONF_DELAY,
    CONF_MODEL,
    CONF_RESET,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_BRIGHTNESS_MODE,
    DEFAULT_DELAY,
    DEFAULT_RESET,
    DOMAIN,
    WEEK_DAYS,
)
from .coordinator import ElkCoordinator
from .elkbledom import BLEDOMInstance
from .model import ensure_models_loaded
from .transport import CharacteristicMissingError, NotConnectedError

LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]


@dataclass
class ELKRuntimeData:
    """Runtime objects stored on the config entry."""

    instance: BLEDOMInstance
    coordinator: ElkCoordinator
    options_snapshot: dict


ELKConfigEntry = ConfigEntry[ELKRuntimeData]

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
SERVICE_SET_RANDOM_COLOR_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional("brightness", default=255): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
    }
)

SERVICE_SET_RGB_COLOR_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("r"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Required("g"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Required("b"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional("brightness", default=255): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
    }
)

SERVICE_SYNC_TIME_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
    }
)

SERVICE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required("hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
        vol.Required("minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
        vol.Optional("enabled", default=True): cv.boolean,
        vol.Optional("days", default=["all"]): vol.All(
            cv.ensure_list, [vol.In(SCHEDULE_DAY_NAMES)]
        ),
    }
)


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
    """Resolve target entity_ids to their BLEDOMInstance objects (deduplicated).

    hass.data now holds the ElkCoordinator per entry (Phase 4); unwrap to the
    underlying instance the services call (sync_time/set_scheduler_*/address).
    """
    ent_reg = er.async_get(hass)
    instances = {}
    for entity_id in entity_ids:
        entity_entry = ent_reg.async_get(entity_id)
        if (
            entity_entry is None
            or entity_entry.platform != DOMAIN
            or not entity_entry.config_entry_id
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_target",
                translation_placeholders={"entity_id": entity_id},
            )
        entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
        runtime = getattr(entry, "runtime_data", None) if entry is not None else None
        if runtime is None or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_target",
                translation_placeholders={"entity_id": entity_id},
            )
        instances[entity_entry.config_entry_id] = runtime.instance
    return list(instances.values())


def _days_to_mask(day_names) -> int:
    """OR a list of WEEK_DAYS member names into a single bitmask."""
    mask = 0
    for name in day_names:
        mask |= WEEK_DAYS[name].value
    return mask


def _first_registry_value(rows: list, attribute: str):
    """Return the first explicitly stored registry value in row order."""
    return next(
        (value for row in rows if (value := getattr(row, attribute, None)) is not None),
        None,
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register integration-wide actions exactly once."""

    async def handle_set_random_color(call: ServiceCall) -> None:
        _instances_for_entities(hass, call.data[ATTR_ENTITY_ID])
        await hass.services.async_call(
            Platform.LIGHT,
            "turn_on",
            {
                ATTR_ENTITY_ID: call.data[ATTR_ENTITY_ID],
                "rgb_color": [random.randint(0, 255) for _ in range(3)],
                "brightness": call.data.get("brightness", 255),
            },
            blocking=True,
        )

    async def handle_set_rgb_color(call: ServiceCall) -> None:
        _instances_for_entities(hass, call.data[ATTR_ENTITY_ID])
        await hass.services.async_call(
            Platform.LIGHT,
            "turn_on",
            {
                ATTR_ENTITY_ID: call.data[ATTR_ENTITY_ID],
                "rgb_color": [call.data["r"], call.data["g"], call.data["b"]],
                "brightness": call.data.get("brightness", 255),
            },
            blocking=True,
        )

    async def _run_instance_calls(calls) -> None:
        results = await asyncio.gather(*calls, return_exceptions=True)
        if error := next((result for result in results if isinstance(result, Exception)), None):
            raise error

    async def handle_sync_time(call: ServiceCall) -> None:
        instances = _instances_for_entities(hass, call.data[ATTR_ENTITY_ID])
        await _run_instance_calls(instance.sync_time() for instance in instances)

    async def handle_schedule_on(call: ServiceCall) -> None:
        mask = _days_to_mask(call.data["days"])
        instances = _instances_for_entities(hass, call.data[ATTR_ENTITY_ID])
        await _run_instance_calls(
            instance.set_scheduler_on(
                mask, call.data["hour"], call.data["minute"], call.data["enabled"]
            )
            for instance in instances
        )

    async def handle_schedule_off(call: ServiceCall) -> None:
        mask = _days_to_mask(call.data["days"])
        instances = _instances_for_entities(hass, call.data[ATTR_ENTITY_ID])
        await _run_instance_calls(
            instance.set_scheduler_off(
                mask, call.data["hour"], call.data["minute"], call.data["enabled"]
            )
            for instance in instances
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_RANDOM_COLOR,
        handle_set_random_color,
        schema=SERVICE_SET_RANDOM_COLOR_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_RGB_COLOR,
        handle_set_rgb_color,
        schema=SERVICE_SET_RGB_COLOR_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_TIME, handle_sync_time, schema=SERVICE_SYNC_TIME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_ON, handle_schedule_on, schema=SERVICE_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_OFF, handle_schedule_off, schema=SERVICE_SCHEDULE_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ELKConfigEntry) -> bool:
    """Set up ElkBLEDOM from a config entry."""
    reset = _entry_option(entry, CONF_RESET, DEFAULT_RESET)
    delay = _entry_option(entry, CONF_DELAY, DEFAULT_DELAY)
    mac = normalize_address(_entry_option(entry, CONF_MAC))
    forced_model = _entry_option(entry, CONF_MODEL)
    if forced_model in (AUTOMATIC_MODEL, ""):
        forced_model = None
    brightness_mode = _entry_option(entry, CONF_BRIGHTNESS_MODE, DEFAULT_BRIGHTNESS_MODE)
    LOGGER.debug(
        "Config: Reset: %s, Delay: %s, Mac: %s, Forced Model: %s, Brightness mode: %s",
        reset,
        delay,
        mac,
        forced_model,
        brightness_mode,
    )

    # Ensure models are loaded (will reuse if already in hass.data)
    await ensure_models_loaded(hass)

    instance: BLEDOMInstance | None = None
    try:
        instance = BLEDOMInstance(
            mac,
            reset,
            delay,
            hass,
            forced_model,
            brightness_mode,
            entry.data.get("name"),
        )
        # Prove GATT connectivity and characteristic compatibility before any
        # entities are forwarded. This also verifies an ESPHome proxy has an
        # active connection slot, not merely an advertisement path.
        await instance.update()
    except asyncio.CancelledError:
        if instance is not None:
            await instance.stop()
        raise
    except (BleakError, CharacteristicMissingError, NotConnectedError, TimeoutError) as err:
        if instance is not None:
            await instance.stop()
        reason = bluetooth.async_address_reachability_diagnostics(
            hass,
            instance.transport.bluetooth_address if instance is not None else mac.upper(),
            BluetoothReachabilityIntent.CONNECTION,
        )
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_unreachable",
            translation_placeholders={"address": mac, "reason": reason},
        ) from err
    # Wrap the instance in a push-only coordinator (no polling: it just
    # re-publishes the optimistic ElkState snapshot on transport events). The
    # first refresh returns the seeded snapshot without any BLE traffic. The
    # coordinator (not the raw instance) is what entities/services resolve.
    coordinator = ElkCoordinator(hass, instance)
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = ELKRuntimeData(
            instance=instance,
            coordinator=coordinator,
            options_snapshot=dict(entry.options or {}),
        )

        # Keep the connectable BLEDevice fresh and drive availability from the strip's
        # advertisements (the canonical HA BLE pattern). Both registrations are torn
        # down automatically on unload via entry.async_on_unload.
        entry.async_on_unload(
            bluetooth.async_register_callback(
                hass,
                instance._async_update_ble,
                BluetoothCallbackMatcher(
                    address=instance.transport.bluetooth_address, connectable=True
                ),
                BluetoothScanningMode.ACTIVE,
            )
        )
        entry.async_on_unload(
            bluetooth.async_track_unavailable(
                hass,
                instance._async_unavailable,
                instance.transport.bluetooth_address,
                connectable=True,
            )
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))

        async def _async_stop(event: Event) -> None:
            """Close the connection."""
            await instance.stop()

        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))

        return True

    except BaseException:
        await coordinator.async_shutdown()
        raise


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Canonicalize addresses while preserving established registry rows."""
    if entry.version > CONFIG_ENTRY_VERSION:
        LOGGER.error("Cannot downgrade config entry version %s", entry.version)
        return False
    raw_address = entry.data.get(CONF_MAC)
    if not raw_address:
        hass.config_entries.async_update_entry(
            entry,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )
        return True

    old_address = str(raw_address)
    canonical = normalize_address(old_address)

    # Config-entry uniqueness is the first preflight gate. Check both unique_id
    # and data because historical versions populated them inconsistently.
    for other_entry in hass.config_entries.async_entries(DOMAIN):
        if other_entry.entry_id == entry.entry_id:
            continue
        if any(
            candidate and addresses_equal(str(candidate), canonical)
            for candidate in (other_entry.unique_id, other_entry.data.get(CONF_MAC))
        ):
            LOGGER.error(
                "Cannot migrate %s: address already belongs to entry %s",
                entry.entry_id,
                other_entry.entry_id,
            )
            return False

    entity_registry = er.async_get(hass)
    entity_entries = list(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    entity_groups: dict[tuple[str, str, str], list] = {}
    for entity_entry in entity_entries:
        new_unique_id = normalize_entity_unique_id(entity_entry.unique_id, old_address, canonical)
        entity_groups.setdefault(
            (entity_entry.domain, entity_entry.platform, new_unique_id), []
        ).append(entity_entry)

    entity_survivors: list[tuple[object, str, dict]] = []
    duplicate_entity_ids: set[str] = set()
    for (entity_domain, platform, new_unique_id), rows in entity_groups.items():
        row_ids = {row.entity_id for row in rows}
        existing_id = entity_registry.async_get_entity_id(entity_domain, platform, new_unique_id)
        if existing_id is not None and existing_id not in row_ids:
            LOGGER.error("Cannot migrate unique ID %s; owned by %s", new_unique_id, existing_id)
            return False

        rows.sort(key=lambda row: (str(getattr(row, "created_at", "9999")), row.entity_id))
        survivor = rows[0]
        aliases = list(survivor.aliases)
        labels = set(survivor.labels)
        categories = dict(survivor.categories)
        # Registry options are domain-specific mappings. Preserve the oldest
        # row's explicit choices, including False/None, and fill missing keys
        # from newer duplicates. Never mutate HA's immutable registry mappings.
        merged_options = {
            domain: dict(options) for domain, options in getattr(survivor, "options", {}).items()
        }
        for duplicate in rows[1:]:
            aliases.extend(alias for alias in duplicate.aliases if alias not in aliases)
            labels.update(duplicate.labels)
            for scope, category in duplicate.categories.items():
                categories.setdefault(scope, category)
            for domain, options in getattr(duplicate, "options", {}).items():
                target_options = merged_options.setdefault(domain, {})
                for key, value in options.items():
                    target_options.setdefault(key, value)

        entity_survivors.append(
            (
                survivor,
                new_unique_id,
                {
                    "aliases": aliases,
                    "area_id": _first_registry_value(rows, "area_id"),
                    "categories": categories,
                    "disabled_by": _first_registry_value(rows, "disabled_by"),
                    "entity_category": _first_registry_value(rows, "entity_category"),
                    "hidden_by": _first_registry_value(rows, "hidden_by"),
                    "icon": _first_registry_value(rows, "icon"),
                    "labels": labels,
                    "name": _first_registry_value(rows, "name"),
                    "options": merged_options,
                },
            )
        )
        duplicate_entity_ids.update(row.entity_id for row in rows[1:])

    device_registry = dr.async_get(hass)
    config_devices = list(dr.async_entries_for_config_entry(device_registry, entry.entry_id))

    def matches_address(device_entry) -> bool:
        return any(
            domain == DOMAIN and addresses_equal(identifier, canonical)
            for domain, identifier in device_entry.identifiers
        ) or any(
            connection_type in (dr.CONNECTION_BLUETOOTH, dr.CONNECTION_NETWORK_MAC)
            and addresses_equal(address, canonical)
            for connection_type, address in device_entry.connections
        )

    address_devices = [device for device in config_devices if matches_address(device)]
    if not address_devices and len(config_devices) == 1:
        address_devices = config_devices
    elif not address_devices and config_devices:
        LOGGER.error("Cannot safely select an address-less device row for %s", entry.entry_id)
        return False

    address_devices.sort(key=lambda device: (str(getattr(device, "created_at", "9999")), device.id))
    address_device_ids = {device.id for device in address_devices}
    target_device = address_devices[0] if address_devices else None
    target_device_id = target_device.id if target_device is not None else None

    # Current HA registry APIs scope identifiers/connections to this config
    # entry. Preflight every value before any removal.
    for lookup in (
        device_registry.async_get_device_by_identifier((DOMAIN, canonical), entry.entry_id),
        device_registry.async_get_device_by_connection(
            (dr.CONNECTION_BLUETOOTH, canonical), entry.entry_id
        ),
    ):
        if lookup is not None and lookup.id not in address_device_ids:
            LOGGER.error("Cannot migrate address %s; device %s owns it", canonical, lookup.id)
            return False

    for duplicate in address_devices[1:]:
        if duplicate.config_entry_id != entry.entry_id:
            LOGGER.error("Cannot consolidate shared device row %s", duplicate.id)
            return False

    merged_identifiers: set[tuple[str, str]] = set()
    merged_connections: set[tuple[str, str]] = set()
    for device in address_devices:
        merged_identifiers.update(
            (DOMAIN, canonical)
            if domain == DOMAIN and addresses_equal(identifier, canonical)
            else (domain, identifier)
            for domain, identifier in device.identifiers
        )
        merged_connections.update(
            (connection_type, address)
            for connection_type, address in device.connections
            if not (
                connection_type in (dr.CONNECTION_BLUETOOTH, dr.CONNECTION_NETWORK_MAC)
                and addresses_equal(address, canonical)
            )
        )
    if target_device is not None:
        merged_identifiers.add((DOMAIN, canonical))
        merged_connections.add((dr.CONNECTION_BLUETOOTH, canonical))

    for identifier in merged_identifiers:
        owner = device_registry.async_get_device_by_identifier(identifier, entry.entry_id)
        if owner is not None and owner.id not in address_device_ids:
            return False
    for connection in merged_connections:
        owner = device_registry.async_get_device_by_connection(connection, entry.entry_id)
        if owner is not None and owner.id not in address_device_ids:
            return False

    if target_device is not None:
        merged_labels = set().union(*(device.labels for device in address_devices))

        def first_device_value(attribute: str):
            if (value := getattr(target_device, attribute, None)) is not None:
                return value
            return next(
                (
                    getattr(duplicate, attribute, None)
                    for duplicate in address_devices[1:]
                    if getattr(duplicate, attribute, None) is not None
                ),
                None,
            )

        device_registry.async_update_device(
            target_device.id,
            area_id=first_device_value("area_id"),
            disabled_by=first_device_value("disabled_by"),
            labels=merged_labels,
            name_by_user=first_device_value("name_by_user"),
        )

    for survivor, _new_unique_id, metadata in entity_survivors:
        options = metadata["options"]
        entity_registry.async_update_entity(
            survivor.entity_id,
            **{key: value for key, value in metadata.items() if key != "options"},
        )
        for domain, domain_options in options.items():
            entity_registry.async_update_entity_options(survivor.entity_id, domain, domain_options)
    for duplicate_entity_id in duplicate_entity_ids:
        entity_registry.async_remove(duplicate_entity_id)

    for survivor, new_unique_id, _metadata in entity_survivors:
        update_kwargs = {}
        if survivor.unique_id != new_unique_id:
            update_kwargs["new_unique_id"] = new_unique_id
        if (
            target_device_id is not None
            and survivor.device_id in address_device_ids
            and survivor.device_id != target_device_id
        ):
            update_kwargs["device_id"] = target_device_id
        if update_kwargs:
            entity_registry.async_update_entity(survivor.entity_id, **update_kwargs)

    if target_device_id is not None:
        for duplicate_device in address_devices[1:]:
            for registry_entity in er.async_entries_for_device(
                entity_registry, duplicate_device.id, include_disabled_entities=True
            ):
                entity_registry.async_update_entity(
                    registry_entity.entity_id, device_id=target_device_id
                )
            device_registry.async_remove_device(duplicate_device.id)
        device_registry.async_update_device(
            target_device_id,
            new_identifiers=merged_identifiers,
            new_connections=merged_connections,
        )

    new_data = dict(entry.data)
    new_data[CONF_MAC] = canonical
    new_options = dict(entry.options or {})
    # Historical entries do not distinguish automatic detection from an
    # explicit generic-model choice. Preserve that choice; users can select
    # Automatic in options to enable handle-based refinement.
    if new_options.get(CONF_MODEL) == "":
        new_options[CONF_MODEL] = AUTOMATIC_MODEL

    hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        unique_id=canonical,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ELKConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            # async_shutdown() unsubscribes from the transport, then stops the
            # underlying instance (closes the BLE connection + cancels timers).
            await runtime.coordinator.async_shutdown()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Make a removed strip discoverable again without restarting Home Assistant."""
    if address := entry.data.get(CONF_MAC):
        canonical = normalize_address(address)
        for candidate in dict.fromkeys((str(address), canonical, canonical.upper())):
            bluetooth.async_rediscover_address(hass, candidate)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update.

    Brightness mode is applied live (no reload, so it isn't reset to "auto").
    A reload is triggered only when an option consumed at setup time (reset,
    delay, forced model) actually changed.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    instance = runtime.instance

    brightness_mode = entry.options.get(CONF_BRIGHTNESS_MODE)
    if brightness_mode:
        await instance.apply_brightness_mode(brightness_mode)

    new_reset = _entry_option(entry, CONF_RESET, DEFAULT_RESET)
    new_delay = _entry_option(entry, CONF_DELAY, DEFAULT_DELAY)
    new_model = _entry_option(entry, CONF_MODEL)
    if new_model in (AUTOMATIC_MODEL, ""):
        new_model = None
    if (
        bool(new_reset) != bool(instance.reset)
        or new_delay != instance.delay
        or new_model != instance.forced_model
    ):
        LOGGER.debug("Reloading %s due to changed setup options", entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
        return
    runtime.options_snapshot = dict(entry.options or {})
    instance.transport.fire_callbacks()
