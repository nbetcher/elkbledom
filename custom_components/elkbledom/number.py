from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import ElkCoordinator
from .entity import BLEDOMEntity

LOG = logging.getLogger(__name__)
PARALLEL_UPDATES = 0  # BLETransport serializes intents for each physical device.


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data.coordinator
    instance = coordinator.instance
    entities = [BLEDOMMicSensitivity(coordinator)]
    if instance.model.get_effect_speed_cmd(instance.model_name, 50):
        entities.append(BLEDOMEffectSpeed(coordinator))
    async_add_entities(entities)


class BLEDOMEffectSpeed(BLEDOMEntity, RestoreEntity, NumberEntity):
    """Effect Speed entity"""

    _attr_translation_key = "effect_speed"

    def __init__(self, coordinator: ElkCoordinator) -> None:
        super().__init__(coordinator)
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
        # The strip's speed byte is a 0-100 percent; values above 100 are out of
        # range for the firmware (the vendor app clamps to 100).
        return 100

    @property
    def native_step(self) -> int:
        return 1

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self._device.set_effect_speed(int(value))
        self._effect_speed = int(value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()

        # Restore the last known effect speed
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._effect_speed = max(0, min(int(float(last_state.state)), 100))
                self._instance.state.restore(effect_speed=self._effect_speed)
                LOG.debug(f"Restored effect speed for {self.name}: {self._effect_speed}")
            except (ValueError, TypeError):
                LOG.debug(f"Could not restore effect speed for {self.name}, using default")


class BLEDOMMicSensitivity(BLEDOMEntity, RestoreEntity, NumberEntity):
    """Microphone Sensitivity entity"""

    _attr_translation_key = "mic_sensitivity"

    def __init__(self, coordinator: ElkCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = self._instance.address + "_mic_sensitivity"
        self._mic_sensitivity = 50
        # Disabled by default unless the model opts into mic support, so
        # unsupported strips don't show a non-functional control.
        self._attr_entity_registry_enabled_default = self._instance.model.get_supports_mic(
            self._instance.model_name
        )

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
        await self._device.set_mic_sensitivity(int(value))
        self._mic_sensitivity = int(value)
        # Publish explicitly: with should_poll=False (CoordinatorEntity) HA no
        # longer auto-writes state after the service call, and set_mic_sensitivity
        # fires no coordinator push, so without this the slider snaps back.
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()

        # Restore the last known mic sensitivity
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._mic_sensitivity = max(0, min(int(float(last_state.state)), 100))
                LOG.debug(f"Restored mic sensitivity for {self.name}: {self._mic_sensitivity}")
            except (ValueError, TypeError):
                LOG.debug(f"Could not restore mic sensitivity for {self.name}, using default (50)")
        else:
            LOG.debug(f"No previous state found for {self.name}")
