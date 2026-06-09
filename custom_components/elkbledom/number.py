from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
)

from .elkbledom import BLEDOMInstance
from .entity import BLEDOMEntity
from .const import DOMAIN

from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry


import logging

LOG = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    instance = hass.data[DOMAIN][config_entry.entry_id]
    entities = [BLEDOMMicSensitivity(instance, "Mic Sensitivity " + config_entry.data["name"], config_entry.entry_id)]
    if instance.model.get_effect_speed_cmd(instance.model_name, 128):
        entities.append(BLEDOMEffectSpeed(instance, "Effect Speed " + config_entry.data["name"], config_entry.entry_id))
    async_add_entities(entities)

class BLEDOMEffectSpeed(BLEDOMEntity, RestoreEntity, NumberEntity):
    """Effect Speed entity"""

    def __init__(self, bledomInstance: BLEDOMInstance, attr_name: str, entry_id: str) -> None:
        self._instance = bledomInstance
        self._attr_name = attr_name
        self._attr_unique_id = self._instance.address + "_effect_speed"
        self._effect_speed = 0

    @property
    def native_value(self) -> int | None:
        # Sync with instance value
        if self._instance.effect_speed is not None:
            return self._instance.effect_speed
        return self._effect_speed

    @property
    def native_min_value(self) -> int:
        return 0

    @property
    def native_max_value(self) -> int:
        return 255

    @property
    def native_step(self) -> int:
        return 1

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self._instance.set_effect_speed(int(value))
        self._effect_speed = int(value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore the last known effect speed
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._effect_speed = int(float(last_state.state))
                LOG.debug(f"Restored effect speed for {self.name}: {self._effect_speed}")
            except (ValueError, TypeError):
                LOG.debug(f"Could not restore effect speed for {self.name}, using default")

class BLEDOMMicSensitivity(BLEDOMEntity, RestoreEntity, NumberEntity):
    """Microphone Sensitivity entity"""

    def __init__(self, bledomInstance: BLEDOMInstance, attr_name: str, entry_id: str) -> None:
        self._instance = bledomInstance
        self._attr_name = attr_name
        self._attr_unique_id = self._instance.address + "_mic_sensitivity"
        self._mic_sensitivity = 50
        # Disabled by default unless the model opts into mic support, so
        # unsupported strips don't show a non-functional control.
        self._attr_entity_registry_enabled_default = self._instance.model.get_supports_mic(self._instance.model_name)

    @property
    def native_value(self) -> int | None:
        return self._mic_sensitivity

    @property
    def native_min_value(self) -> int:
        return 0

    @property
    def native_max_value(self) -> int:
        return 100

    @property
    def native_step(self) -> int:
        return 1

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self._instance.set_mic_sensitivity(int(value))
        self._mic_sensitivity = int(value)

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Restore the last known mic sensitivity
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._mic_sensitivity = int(float(last_state.state))
                LOG.debug(f"Restored mic sensitivity for {self.name}: {self._mic_sensitivity}")
            except (ValueError, TypeError):
                LOG.debug(f"Could not restore mic sensitivity for {self.name}, using default (50)")
        else:
            LOG.debug(f"No previous state found for {self.name}")
