from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([BLEDOMMicSwitch(coordinator)])


class BLEDOMMicSwitch(BLEDOMEntity, RestoreEntity, SwitchEntity):
    """Microphone (music-mode) enable switch.

    The strip's real music-mode flag lives on the instance and is ALSO cleared
    automatically whenever a normal color/effect/brightness command runs (via
    _exit_mic_mode). This switch therefore reflects ``self._instance.mic_enabled``
    as the single source of truth instead of tracking its own copy, which used to
    drift out of sync (show OFF after selecting a mic effect, or stay stuck ON
    after a color change silently left music mode). The transport fires a state
    callback on every mic transition; the ElkCoordinator re-publishes the snapshot
    and CoordinatorEntity refreshes this switch, so it updates even when the change
    came from a light command rather than the switch itself.
    """

    # Optimistic (no readback): show an explicit on/off control.
    _attr_assumed_state = True
    _attr_translation_key = "microphone"

    def __init__(self, coordinator: ElkCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = self._instance.address + "_mic_enable"
        # Disabled by default unless the model opts into mic support.
        self._attr_entity_registry_enabled_default = self._instance.model.get_supports_mic(
            self._instance.model_name
        )

    @property
    def is_on(self) -> bool:
        return self._instance.mic_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the microphone on."""
        await self._device.enable_mic()
        LOG.debug(f"Microphone enabled for {self.name}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the microphone off."""
        await self._device.disable_mic()
        LOG.debug(f"Microphone disabled for {self.name}")

    async def async_added_to_hass(self) -> None:
        """Subscribe to state pushes and best-effort restore the mic flag."""
        await super().async_added_to_hass()
        # If we were mic-on before a restart, seed the instance flag so the next
        # normal command still emits an explicit mic-off (harmless if the strip
        # was power-cycled out of music mode -- mic-off on a static strip is a
        # no-op).
        if (
            last_state := await self.async_get_last_state()
        ) is not None and last_state.state == "on":
            self._instance.state.restore(mic_enabled=True)
            LOG.debug(f"Restored mic state for {self.name}: ON")
