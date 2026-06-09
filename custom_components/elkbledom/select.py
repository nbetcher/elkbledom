from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .elkbledom import BLEDOMInstance
from .entity import BLEDOMEntity
from .const import DOMAIN, MIC_EFFECTS, MIC_EFFECTS_list, BRIGHTNESS_MODES, CONF_BRIGHTNESS_MODE

import logging

LOG = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    instance = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([
        BLEDOMMicEffect(instance, "Mic Effect " + config_entry.data["name"], config_entry.entry_id),
        BLEDOMBrightnessModeSelect(instance, "Brightness Mode " + config_entry.data["name"], config_entry, config_entry.entry_id)
    ])

class BLEDOMMicEffect(BLEDOMEntity, RestoreEntity, SelectEntity):
    """Microphone Effect selector entity"""

    def __init__(self, bledomInstance: BLEDOMInstance, attr_name: str, entry_id: str) -> None:
        self._instance = bledomInstance
        self._attr_name = attr_name
        self._attr_unique_id = self._instance.address + "_mic_effect"
        self._current_option = MIC_EFFECTS_list[0]
        # Disabled by default unless the model opts into mic support.
        self._attr_entity_registry_enabled_default = self._instance.model.get_supports_mic(self._instance.model_name)

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @property
    def options(self) -> list[str]:
        return MIC_EFFECTS_list

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in MIC_EFFECTS_list:
            effect_value = MIC_EFFECTS[option].value
            await self._instance.set_mic_effect(effect_value)
            self._current_option = option
            LOG.debug(f"Mic effect set to {option} (0x{effect_value:02x})")

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore the last known mic effect
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state in MIC_EFFECTS_list:
                self._current_option = last_state.state
                LOG.debug(f"Restored mic effect for {self.name}: {self._current_option}")
            else:
                LOG.debug(f"Could not restore mic effect for {self.name}, using default")


class BLEDOMBrightnessModeSelect(BLEDOMEntity, RestoreEntity, SelectEntity):
    """Brightness Mode selector entity"""

    def __init__(self, bledomInstance: BLEDOMInstance, attr_name: str, entry: ConfigEntry, entry_id: str) -> None:
        self._instance = bledomInstance
        self._entry = entry
        self._attr_name = attr_name
        self._attr_unique_id = self._instance.address + "_brightness_mode"
        self._current_option = entry.options.get(CONF_BRIGHTNESS_MODE, "auto")

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @property
    def options(self) -> list[str]:
        return BRIGHTNESS_MODES

    async def async_select_option(self, option: str) -> None:
        """Change the selected brightness mode."""
        if option not in BRIGHTNESS_MODES:
            LOG.warning("Invalid brightness mode selected: %s", option)
            return

        if option == self._current_option:
            return  # Nothing to change

        LOG.info("Changing brightness mode to %s for %s", option, self._instance.address)
        self._current_option = option

        # Update HA entry options
        data = dict(self._entry.options)
        data[CONF_BRIGHTNESS_MODE] = option
        self.hass.config_entries.async_update_entry(self._entry, options=data)

        # Apply mode to instance
        await self._instance.apply_brightness_mode(option)

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore the last known brightness mode
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state in BRIGHTNESS_MODES:
                self._current_option = last_state.state
                LOG.debug(f"Restored brightness mode for {self.name}: {self._current_option}")
            else:
                LOG.debug(f"Could not restore brightness mode for {self.name}, using default")

        # Make sure the running instance actually reflects the selected mode.
        # (Setup also seeds it from the config entry; this covers the restore path.)
        await self._instance.apply_brightness_mode(self._current_option)

