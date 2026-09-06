import asyncio
import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_MAC
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .address import normalize_address
from .const import (
    AUTOMATIC_MODEL,
    CONF_DELAY,
    CONF_EFFECTS_CLASS,
    CONF_MODEL,
    CONF_RESET,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_DELAY,
    DEFAULT_RESET,
    DOMAIN,
    EFFECTS_LIST_MAP,
    EFFECTS_MAP,
    MAX_DELAY,
    MIN_DELAY,
    effects_list_name_for_class,
)
from .elkbledom import BLEDOMInstance, DeviceData
from .model import Model, ensure_models_loaded

LOGGER = logging.getLogger(__name__)
DATA_SCHEMA = vol.Schema({("host"): str})

MANUAL_MAC = "manual"
AUTOMATIC_MODEL_LABEL = "Automatic"
MAC_REGEX = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$")
UUID_REGEX = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Connection timeout (idle disconnect, seconds). Coerce to int and clamp into
# [MIN_DELAY, MAX_DELAY] so an out-of-range entry is silently capped rather than
# rejected. 0 keeps the connection alive (never disconnect).
DELAY_VALIDATOR = vol.All(vol.Coerce(int), vol.Clamp(min=MIN_DELAY, max=MAX_DELAY))


def _is_valid_address(value: str) -> bool:
    """Return whether a manual Bluetooth address is a MAC or CoreBluetooth UUID."""
    value = value.strip()
    return bool(value and (MAC_REGEX.fullmatch(value) or UUID_REGEX.fullmatch(value)))


def _normalized_current_ids(flow: config_entries.ConfigFlow) -> set[str]:
    """Return current config-entry IDs in canonical form."""
    current = {normalize_address(unique_id) for unique_id in flow._async_current_ids() if unique_id}
    current.update(
        normalize_address(address)
        for entry in flow._async_current_entries()
        if (address := entry.data.get(CONF_MAC))
    )
    return current


def _valid_effect_classes() -> dict[str, str]:
    """Only expose effects classes whose names all have encoder values."""
    valid = {}
    for class_name, effect_enum in EFFECTS_MAP.items():
        effect_list = EFFECTS_LIST_MAP.get(effects_list_name_for_class(class_name))
        if effect_list is not None and set(effect_list).issubset(effect_enum.__members__):
            valid[class_name] = class_name
    return valid


class BLEDOMFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    def __init__(self) -> None:
        self.mac = None
        self._device = None
        self._instance = None
        self.name = None
        self._model_name = None
        self._model_is_explicit = False
        self._effects_class = None
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices = []

    #    async def async_step_bluetooth(
    #        self, discovery_info: BluetoothServiceInfoBleak
    #    ) -> FlowResult:
    #        """Handle the bluetooth discovery step."""
    #        await self.async_set_unique_id(discovery_info.address)
    #        self._abort_if_unique_id_configured()
    #        device = DeviceData(self.hass, discovery_info)
    #        if device.is_supported:
    #            self._discovered_devices.append(device)
    #            return await self.async_step_bluetooth_confirm()
    #        else:
    #            return self.async_abort(reason="not_supported")

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> FlowResult:
        LOGGER.debug(
            "Discovered device: address=%s, name=%s", discovery_info.address, discovery_info.name
        )
        if not discovery_info.address:
            LOGGER.error("Invalid discovery info (no address): %s", discovery_info)
            return self.async_abort(reason="invalid_discovery_info")

        if not discovery_info.name:
            LOGGER.warning(
                "Device discovered without name: %s, aborting discovery", discovery_info.address
            )
            return self.async_abort(reason="no_device_name")
        if not discovery_info.connectable:
            return self.async_abort(reason="not_connectable")

        canonical_address = normalize_address(discovery_info.address)
        if canonical_address in _normalized_current_ids(self):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(canonical_address)
        self._abort_if_unique_id_configured()
        await ensure_models_loaded(self.hass)
        device = DeviceData(self.hass, discovery_info)
        LOGGER.info(
            "Device %s (%s) - Supported: %s",
            discovery_info.name,
            discovery_info.address,
            device.is_supported,
        )
        if device.is_supported:
            self.mac = canonical_address
            self.name = discovery_info.name
            self._device = device
            self._discovery_info = discovery_info
            self._discovered_devices.append(device)
            self.context["title_placeholders"] = {"name": self.name}
            return await self.async_step_bluetooth_confirm()
        LOGGER.info("Device not supported for auto-discovery: %s", discovery_info.name)
        return self.async_abort(reason="not_supported")

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        if user_input is not None:
            model_manager = Model(self.hass)
            self._model_name = model_manager.detect_model(self._discovery_info.name or "")
            self._model_is_explicit = False
            self._effects_class = None
            return await self.async_step_validate()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self.name or self.mac},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            if user_input[CONF_MAC] == MANUAL_MAC:
                return await self.async_step_manual()
            self.mac = normalize_address(user_input[CONF_MAC])
            self.name = user_input["name"]
            if self.mac in _normalized_current_ids(self):
                return self.async_abort(reason="already_configured")
            await self.async_set_unique_id(self.mac)
            self._abort_if_unique_id_configured()
            # Ensure models are loaded before auto-detect
            await ensure_models_loaded(self.hass)
            # Auto-detect model from Bluetooth device name (not user-given name)
            model_manager = Model(self.hass)
            available_models_count = len(model_manager.get_models())
            LOGGER.debug("Available models in manager: %d", available_models_count)

            # Find the device's Bluetooth name from discovered devices
            bluetooth_device_name = None
            for device in self._discovered_devices:
                if normalize_address(device.address) == self.mac:
                    bluetooth_device_name = device.name
                    break

            LOGGER.debug(
                "Bluetooth device name: %s, User-given name: %s", bluetooth_device_name, self.name
            )
            self._model_name = model_manager.detect_model(bluetooth_device_name or "")
            self._model_is_explicit = False
            LOGGER.debug(
                "Auto-detected model: %s for device: %s (from %d available models)",
                self._model_name,
                bluetooth_device_name,
                available_models_count,
            )
            # Name-only detection is provisional. GATT handle discovery may
            # refine the ELK-BLEDOM protocol, so do not persist it as forced.
            self._effects_class = None
            if not self._model_name:
                LOGGER.warning(
                    "No model detected for Bluetooth device: %s (user name: %s)",
                    bluetooth_device_name,
                    self.name,
                )
            return await self.async_step_validate()

        await ensure_models_loaded(self.hass)
        current_addresses = _normalized_current_ids(self)
        discovered_devices = async_discovered_service_info(self.hass, connectable=True)
        for discovery_info in discovered_devices:
            self.mac = normalize_address(discovery_info.address)
            if self.mac in current_addresses:
                LOGGER.debug("Device %s in current_addresses", (self.mac))
                continue
            if any(
                normalize_address(device.address) == self.mac for device in self._discovered_devices
            ):
                LOGGER.debug(
                    "Device with address %s in discovered_devices, discarting duplicates", self.mac
                )
                continue
            device = DeviceData(self.hass, discovery_info)
            LOGGER.debug(
                "Checking device %s (%s) - Supported: %s",
                discovery_info.name,
                discovery_info.address,
                device.is_supported,
            )
            if device.is_supported:
                self._discovered_devices.append(device)

        if not self._discovered_devices:
            LOGGER.debug("No supported devices discovered, showing manual setup")
            return await self.async_step_manual()

        for device in self._discovered_devices:
            LOGGER.debug(
                "Discovered supported devices: %s - %s - %s",
                device.name,
                device.address,
                device.rssi,
            )

        mac_dict = {dev.address: dev.name for dev in self._discovered_devices}
        mac_dict[MANUAL_MAC] = "Manually add a MAC address"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_MAC): vol.In(mac_dict), vol.Required("name"): str}
            ),
            errors={},
        )

    async def async_step_validate(self, user_input: dict[str, Any] | None = None):
        # Always prove a usable GATT path. A form submission cannot bypass this
        # check and create an advertisement-only proxy entry.
        error = await self.validate_connection()

        if error:
            return self.async_show_form(
                step_id="validate",
                data_schema=vol.Schema({}),
                errors={"base": "connect"},
            )

        return await self.async_step_settings()

    async def async_step_settings(self, user_input: dict[str, Any] | None = None):
        """Collect per-device connection settings, then create the entry."""
        if user_input is not None:
            entry_data = {CONF_MAC: self.mac, "name": self.name}
            if self._model_is_explicit and self._model_name:
                entry_data[CONF_MODEL] = self._model_name
                LOGGER.debug("Saving model to entry_data: %s", self._model_name)
            if self._effects_class:
                entry_data[CONF_EFFECTS_CLASS] = self._effects_class
                LOGGER.debug("Saving effects_class to entry_data: %s", self._effects_class)
            # Store the timeout in options (where the OptionsFlow also manages it)
            # so the two never diverge; _entry_option reads options then data.
            delay = user_input[CONF_DELAY]
            LOGGER.debug("Creating entry with data: %s, delay: %s", entry_data, delay)
            return self.async_create_entry(
                title=self.name,
                data=entry_data,
                options={CONF_DELAY: delay},
            )

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {vol.Required(CONF_DELAY, default=DEFAULT_DELAY): DELAY_VALIDATOR}
            ),
            errors={},
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            raw_address = user_input[CONF_MAC].strip()
            if not _is_valid_address(raw_address):
                errors[CONF_MAC] = "invalid_mac"
            else:
                self.mac = normalize_address(raw_address)
                self.name = user_input["name"]
                selected_model = user_input.get(CONF_MODEL, AUTOMATIC_MODEL)
                self._model_is_explicit = selected_model != AUTOMATIC_MODEL
                self._model_name = selected_model if self._model_is_explicit else None
                self._effects_class = user_input.get(CONF_EFFECTS_CLASS)
                if self.mac in _normalized_current_ids(self):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(self.mac)
                self._abort_if_unique_id_configured()
                return await self.async_step_validate()

        # Ensure models are loaded
        await ensure_models_loaded(self.hass)
        # Get available models
        model_manager = Model(self.hass)
        available_models = model_manager.get_models()
        LOGGER.debug(
            "Manual setup - Available models: %d - %s", len(available_models), available_models
        )
        models_dict = {
            AUTOMATIC_MODEL: AUTOMATIC_MODEL_LABEL,
            **model_manager.get_models_display_dict(),
        }

        if not models_dict:
            LOGGER.error("No models available in manual setup! Check if models.json is loaded.")

        # Create effects class selector
        effects_classes_dict = _valid_effect_classes()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): str,
                    vol.Required("name"): str,
                    vol.Required(CONF_MODEL, default=AUTOMATIC_MODEL): vol.In(models_dict),
                    vol.Optional(CONF_EFFECTS_CLASS): vol.In(effects_classes_dict),
                }
            ),
            errors=errors,
        )

    async def validate_connection(self):
        """Validate GATT connectivity without changing the user's light."""
        try:
            if not self._instance:
                forced_model = self._model_name if self._model_is_explicit else None
                self._instance = BLEDOMInstance(self.mac, False, 2, self.hass, forced_model)
            await asyncio.wait_for(self._instance.update(), timeout=20)
        except Exception as error:  # noqa: BLE001 - surface all validation failures
            LOGGER.error("Error during connection validation: %s", error)
            return error
        finally:
            if self._instance:
                try:
                    await self._instance.stop()
                except Exception:  # noqa: BLE001 - preserve the original validation error
                    LOGGER.debug("Failed to stop validation connection", exc_info=True)
                # Drop the throwaway validation instance so nothing lingers past
                # the flow (stop() also cancels its idle timer/task).
                self._instance = None

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    # `self.config_entry` is provided by the OptionsFlow base class in modern HA.

    async def async_step_init(self, _user_input=None):
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        errors = {}
        current_model = self.config_entry.options.get(CONF_MODEL) or self.config_entry.data.get(
            CONF_MODEL
        )

        if user_input is not None:
            # Start from existing options so settings not shown in this form
            # (e.g. brightness_mode, managed by the Brightness Mode select entity)
            # are preserved instead of being wiped on save.
            new_options = dict(self.config_entry.options)
            new_options[CONF_RESET] = user_input[CONF_RESET]
            new_options[CONF_DELAY] = user_input[CONF_DELAY]
            if CONF_MODEL in user_input:
                # Value is already internal_key from vol.In
                new_options[CONF_MODEL] = user_input[CONF_MODEL]
            if CONF_EFFECTS_CLASS in user_input:
                new_options[CONF_EFFECTS_CLASS] = user_input[CONF_EFFECTS_CLASS]
            return self.async_create_entry(title="", data=new_options)

        # Ensure models are loaded
        await ensure_models_loaded(self.hass)
        # Get available models
        model_manager = Model(self.hass)
        models_dict = {
            AUTOMATIC_MODEL: AUTOMATIC_MODEL_LABEL,
            **model_manager.get_models_display_dict(),
        }

        # Get default effects class based on current model
        current_effects_class = self.config_entry.options.get(
            CONF_EFFECTS_CLASS
        ) or self.config_entry.data.get(CONF_EFFECTS_CLASS)
        if not current_effects_class and current_model:
            # Get default from model configuration
            current_effects_class = model_manager.get_effects_class(current_model)

        # Create effects class selector
        effects_classes_dict = _valid_effect_classes()

        schema_dict = {
            vol.Optional(
                CONF_RESET,
                default=self.config_entry.options.get(
                    CONF_RESET, self.config_entry.data.get(CONF_RESET, DEFAULT_RESET)
                ),
            ): bool,
            vol.Optional(
                CONF_DELAY,
                default=self.config_entry.options.get(
                    CONF_DELAY, self.config_entry.data.get(CONF_DELAY, DEFAULT_DELAY)
                ),
            ): DELAY_VALIDATOR,
        }

        # Add model selector if models are available
        if models_dict:
            if current_model and current_model in models_dict:
                # Use default to pre-select current model
                schema_dict[vol.Optional(CONF_MODEL, default=current_model)] = vol.In(models_dict)
            else:
                schema_dict[vol.Optional(CONF_MODEL, default=AUTOMATIC_MODEL)] = vol.In(models_dict)

        # Add effects class selector
        if effects_classes_dict:
            if current_effects_class in effects_classes_dict:
                # Use default to pre-select current effects class
                schema_dict[vol.Optional(CONF_EFFECTS_CLASS, default=current_effects_class)] = (
                    vol.In(effects_classes_dict)
                )
            else:
                schema_dict[vol.Optional(CONF_EFFECTS_CLASS)] = vol.In(effects_classes_dict)

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(schema_dict), errors=errors
        )
