from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .elkbledom import BLEDOMInstance
from .entity import BLEDOMEntity
from .const import DOMAIN

import logging

LOG = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    instance = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([
        BLEDOMMicSwitch(instance, "Mic Enable " + config_entry.data["name"], config_entry.entry_id)
    ])

class BLEDOMMicSwitch(BLEDOMEntity, RestoreEntity, SwitchEntity):
    """Microphone Enable/Disable switch entity"""

    def __init__(self, bledomInstance: BLEDOMInstance, attr_name: str, entry_id: str) -> None:
        self._instance = bledomInstance
        self._attr_name = attr_name
        self._attr_unique_id = self._instance.address + "_mic_enable"
        self._is_on = False
        # Disabled by default unless the model opts into mic support.
        self._attr_entity_registry_enabled_default = self._instance.model.get_supports_mic(self._instance.model_name)

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the microphone on."""
        await self._instance.enable_mic()
        self._is_on = True
        LOG.debug(f"Microphone enabled for {self.name}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the microphone off."""
        await self._instance.disable_mic()
        self._is_on = False
        LOG.debug(f"Microphone disabled for {self.name}")

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore the last known mic state
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state == "on":
                self._is_on = True
                LOG.debug(f"Restored mic state for {self.name}: ON")
            elif last_state.state == "off":
                self._is_on = False
                LOG.debug(f"Restored mic state for {self.name}: OFF")
        else:
            LOG.debug(f"No previous mic state found for {self.name}, defaulting to OFF")
