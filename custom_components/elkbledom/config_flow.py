import asyncio
from .elkbledom import BLEDOMInstance
from .elkbledom import DeviceData
from .model import Model, ensure_models_loaded
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_MAC
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResult
from homeassistant.core import callback
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)

from .const import DOMAIN, CONF_RESET, CONF_DELAY, CONF_MODEL, CONF_EFFECTS_CLASS, EFFECTS_MAP, DEFAULT_RESET, DEFAULT_DELAY, MIN_DELAY, MAX_DELAY
import logging

LOGGER = logging.getLogger(__name__)
DATA_SCHEMA = vol.Schema({("host"): str})

MANUAL_MAC = "manual"

# Connection timeout (idle disconnect, seconds). Coerce to int and clamp into
# [MIN_DELAY, MAX_DELAY] so an out-of-range entry is silently capped rather than
# rejected. 0 keeps the connection alive (never disconnect).
DELAY_VALIDATOR = vol.All(vol.Coerce(int), vol.Clamp(min=MIN_DELAY, max=MAX_DELAY))

class BLEDOMFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        self.mac = None
        self._device = None
        self._instance = None
        self.name = None
        self._model_name = None
        self._effects_class = None
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices = []

#    async def async_step_bluetooth(
#        self, discovery_info: BluetoothServiceInfoBleak
#    ) -> FlowResult:
#        """Handle the bluetooth discovery step."""
#        LOGGER.debug("Discovered bluetooth devices, step bluetooth, : %s , %s", discovery_info.address, discovery_info.name)
#        await self.async_set_unique_id(discovery_info.address)
#        self._abort_if_unique_id_configured()
#        device = DeviceData(self.hass, discovery_info)
#        if device.is_supported:
#            self._discovered_devices.append(device)
#            return await self.async_step_bluetooth_confirm()
#        else:
#            return self.async_abort(reason="not_supported")

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        LOGGER.debug("Discovered device: address=%s, name=%s", discovery_info.address, discovery_info.name)
        if not discovery_info.address:
            LOGGER.error("Invalid discovery info (no address): %s", discovery_info)
            return self.async_abort(reason="invalid_discovery_info")
        
        if not discovery_info.name:
            LOGGER.warning("Device discovered without name: %s, aborting discovery", discovery_info.address)
            return self.async_abort(reason="no_device_name")

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = DeviceData(self.hass, discovery_info)
        LOGGER.info("Device %s (%s) - Supported: %s", discovery_info.name, discovery_info.address, device.is_supported)
        if device.is_supported:
            self._discovered_devices.append(device)
            return await self.async_step_bluetooth_confirm()
        else:
            LOGGER.info("Device not supported for auto-discovery: %s", discovery_info.name)
            return self.async_abort(reason="not_supported")



    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        LOGGER.debug("Discovered bluetooth devices, step bluetooth confirm, : %s", user_input)
        self._set_confirm_only()
        return await self.async_step_user()
    
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            if user_input[CONF_MAC] == MANUAL_MAC:
                return await self.async_step_manual()
            self.mac = user_input[CONF_MAC]
            self.name = user_input["name"]
            # Ensure models are loaded before auto-detect
            await ensure_models_loaded(self.hass)
            # Auto-detect model from Bluetooth device name (not user-given name)
            model_manager = Model(self.hass)
            available_models_count = len(model_manager.get_models())
            LOGGER.debug("Available models in manager: %d", available_models_count)
            
            # Find the device's Bluetooth name from discovered devices
            bluetooth_device_name = None
            for device in self._discovered_devices:
                if device.address == self.mac:
                    bluetooth_device_name = device.name
                    break
            
            LOGGER.debug("Bluetooth device name: %s, User-given name: %s", bluetooth_device_name, self.name)
            self._model_name = model_manager.detect_model(bluetooth_device_name or "")
            LOGGER.debug("Auto-detected model: %s for device: %s (from %d available models)", self._model_name, bluetooth_device_name, available_models_count)
            # Also detect effects class for auto-detected model
            self._effects_class = None
            if self._model_name:
                self._effects_class = model_manager.get_effects_class(self._model_name)
                LOGGER.debug("Auto-detected effects class: %s for model: %s", self._effects_class, self._model_name)
            else:
                LOGGER.warning("No model detected for Bluetooth device: %s (user name: %s)", bluetooth_device_name, self.name)
            result = await self.async_set_unique_id(self.mac, raise_on_progress=False)
            if result is not None:
                return self.async_abort(reason="already_in_progress")
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        current_addresses = self._async_current_ids()
        discovered_devices = async_discovered_service_info(self.hass)
        for discovery_info in discovered_devices:
            self.mac = discovery_info.address
            if self.mac in current_addresses:
                LOGGER.debug("Device %s in current_addresses", (self.mac))
                continue
            if any(device.address == self.mac for device in self._discovered_devices):
                LOGGER.debug("Device with address %s in discovered_devices, discarting duplicates", self.mac)
                continue
            device = DeviceData(self.hass, discovery_info)
            LOGGER.debug("Checking device %s (%s) - Supported: %s", discovery_info.name, discovery_info.address, device.is_supported)
            if device.is_supported:
                self._discovered_devices.append(device)
        
        if not self._discovered_devices:
            LOGGER.debug("No supported devices discovered, showing manual setup")
            return await self.async_step_manual()

        for device in self._discovered_devices:
            LOGGER.debug("Discovered supported devices: %s - %s - %s", device.name, device.address, device.rssi)

        mac_dict = { dev.address: dev.name for dev in self._discovered_devices }
        mac_dict[MANUAL_MAC] = "Manually add a MAC address"
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): vol.In(mac_dict),
                    vol.Required("name"): str
                }
            ),
            errors={})

    async def async_step_validate(self, user_input: "dict[str, Any] | None" = None):
        if user_input is not None:
            if "flicker" in user_input:
                if user_input["flicker"]:
                    # Connection verified -- collect per-device settings
                    # (connection timeout) before creating the entry.
                    return await self.async_step_settings()
                # Light didn't blink -- or the box was left unchecked by accident.
                # Re-run validation (re-toggle and ask again) instead of hard
                # aborting, so a single misclick doesn't drop the discovered device.
                LOGGER.debug("Flicker not confirmed; re-running validation toggle")
                return await self.async_step_validate()

            if "retry" in user_input and not user_input["retry"]:
                return self.async_abort(reason="cannot_connect")

        error = await self.toggle_light()

        if error:
            return self.async_show_form(
                step_id="validate", data_schema=vol.Schema(
                    {
                        vol.Required("retry"): bool
                    }
                ), errors={"base": "connect"})
        
        return self.async_show_form(
            step_id="validate", data_schema=vol.Schema(
                {
                    vol.Required("flicker"): bool
                }
            ), errors={})

    async def async_step_settings(self, user_input: "dict[str, Any] | None" = None):
        """Collect per-device connection settings, then create the entry."""
        if user_input is not None:
            entry_data = {CONF_MAC: self.mac, "name": self.name}
            if self._model_name:
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
            step_id="settings", data_schema=vol.Schema(
                {
                    vol.Required(CONF_DELAY, default=DEFAULT_DELAY): DELAY_VALIDATOR
                }
            ), errors={})

    async def async_step_manual(self, user_input: "dict[str, Any] | None" = None):
        if user_input is not None:            
            self.mac = user_input[CONF_MAC]
            self.name = user_input["name"]
            self._model_name = user_input.get(CONF_MODEL)
            self._effects_class = user_input.get(CONF_EFFECTS_CLASS)
            LOGGER.debug("Manual setup - MAC: %s, Name: %s, Model: %s, Effects: %s", self.mac, self.name, self._model_name, self._effects_class)
            # Use the raw address (same normalization as the discovery/user paths
            # and HA's Bluetooth convention) so the same device can't be added twice.
            await self.async_set_unique_id(self.mac)
            # Abort if this device is already configured (the discovery/user path
            # does this too); without it a manual add creates a duplicate entry.
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        # Ensure models are loaded
        await ensure_models_loaded(self.hass)
        # Get available models
        model_manager = Model(self.hass)
        available_models = model_manager.get_models()
        LOGGER.debug("Manual setup - Available models: %d - %s", len(available_models), available_models)
        models_dict = model_manager.get_models_display_dict()
        
        if not models_dict:
            LOGGER.error("No models available in manual setup! Check if models.json is loaded.")
        
        # Create effects class selector
        effects_classes_dict = {class_name: class_name for class_name in EFFECTS_MAP.keys()}
        
        return self.async_show_form(
            step_id="manual", data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): str,
                    vol.Required("name"): str,
                    vol.Required(CONF_MODEL): vol.In(models_dict),
                    vol.Optional(CONF_EFFECTS_CLASS): vol.In(effects_classes_dict)
                }
            ), errors={})

    async def toggle_light(self):
        try:
            if not self._instance:
                self._instance = BLEDOMInstance(self.mac, False, 120, self.hass, self._model_name)
            # Update to get current state
            await self._instance.update()
            # Toggle the light to verify connection
            if self._instance.is_on:
                await self._instance.turn_off()
                await asyncio.sleep(2)
                await self._instance.turn_on()
                await asyncio.sleep(2)
                await self._instance.turn_off()
            else:
                await self._instance.turn_on()
                await asyncio.sleep(2)
                await self._instance.turn_off()
        except Exception as error:
            LOGGER.error("Error during light toggle: %s", error)
            return error
        finally:
            if self._instance:
                await self._instance.stop()

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
        current_model = self.config_entry.options.get(CONF_MODEL) or self.config_entry.data.get(CONF_MODEL)
        
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
        models_dict = model_manager.get_models_display_dict()
        
        # Get default effects class based on current model
        current_effects_class = self.config_entry.options.get(CONF_EFFECTS_CLASS) or self.config_entry.data.get(CONF_EFFECTS_CLASS)
        if not current_effects_class and current_model:
            # Get default from model configuration
            current_effects_class = model_manager.get_effects_class(current_model)
        
        # Create effects class selector
        effects_classes_dict = {class_name: class_name for class_name in EFFECTS_MAP.keys()}
        
        schema_dict = {
            vol.Optional(CONF_RESET, default=self.config_entry.options.get(CONF_RESET, DEFAULT_RESET)): bool,
            vol.Optional(CONF_DELAY, default=self.config_entry.options.get(CONF_DELAY, DEFAULT_DELAY)): DELAY_VALIDATOR,
        }
        
        # Add model selector if models are available
        if models_dict:
            if current_model and current_model in models_dict:
                # Use default to pre-select current model
                schema_dict[vol.Optional(CONF_MODEL, default=current_model)] = vol.In(models_dict)
            else:
                schema_dict[vol.Optional(CONF_MODEL)] = vol.In(models_dict)
        
        # Add effects class selector
        if effects_classes_dict:
            if current_effects_class:
                # Use default to pre-select current effects class
                schema_dict[vol.Optional(CONF_EFFECTS_CLASS, default=current_effects_class)] = vol.In(effects_classes_dict)
            else:
                schema_dict[vol.Optional(CONF_EFFECTS_CLASS)] = vol.In(effects_classes_dict)
        
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema_dict),
            errors=errors
        )
