from __future__ import annotations

import logging

from homeassistant.components.number import RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    if instance.model.supports_effect_speed(instance.model_name):
        entities.append(BLEDOMEffectSpeed(coordinator))
    async_add_entities(entities)


async def _async_last_native_value(entity: RestoreNumber) -> float | str | None:
    """Prefer native recorder data; migrate older, unitless number records once."""
    if (data := await entity.async_get_last_number_data()) is not None:
        return data.native_value
    if (state := await entity.async_get_last_state()) is not None and not state.attributes.get(
        "unit_of_measurement"
    ):
        return state.state
    return None


class BLEDOMEffectSpeed(BLEDOMEntity, RestoreNumber):
    """Effect Speed entity"""

    _attr_translation_key = "effect_speed"

    def __init__(self, coordinator: ElkCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = self._instance.address + "_effect_speed"
        self._effect_speed = 0
        self._device.restore_effect_speed(self._instance.effect_speed)

    @property
    def native_value(self) -> int | None:
        # Sync with instance value
        if self._instance.effect_speed is not None:
            return self._instance.effect_speed
        return self._effect_speed

    @property
    def native_min_value(self) -> int:
        return self._instance.model.get_effect_speed_limits(self._instance.model_name).minimum

    @property
    def native_max_value(self) -> int:
        return self._instance.model.get_effect_speed_limits(self._instance.model_name).maximum

    @property
    def native_step(self) -> int:
        return 1

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self._device.set_effect_speed(value)
        self._effect_speed = self._instance.effect_speed
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()

        if (value := await _async_last_native_value(self)) is not None:
            self._device.restore_effect_speed(value, from_number=True)
            self._effect_speed = self._instance.effect_speed
            LOG.debug("Restored effect speed for %s: %s", self.name, self._effect_speed)


class BLEDOMMicSensitivity(BLEDOMEntity, RestoreNumber):
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
        if (value := await _async_last_native_value(self)) is not None:
            try:
                self._mic_sensitivity = max(0, min(int(float(value)), 100))
                LOG.debug(f"Restored mic sensitivity for {self.name}: {self._mic_sensitivity}")
            except (ValueError, TypeError, OverflowError):
                LOG.debug(f"Could not restore mic sensitivity for {self.name}, using default (50)")
        else:
            LOG.debug(f"No previous state found for {self.name}")
