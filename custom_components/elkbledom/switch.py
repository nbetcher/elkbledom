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
    """Microphone (music-mode) enable switch.

    The strip's real music-mode flag lives on the instance and is ALSO cleared
    automatically whenever a normal color/effect/brightness command runs (via
    _exit_mic_mode). This switch therefore reflects ``self._instance.mic_enabled``
    as the single source of truth instead of tracking its own copy, which used to
    drift out of sync (show OFF after selecting a mic effect, or stay stuck ON
    after a color change silently left music mode). The instance fires a state
    callback -- subscribed in BLEDOMEntity.async_added_to_hass -- on every mic
    transition so this switch updates even when the change came from a light
    command rather than the switch itself.
    """

    # Optimistic (no readback): show an explicit on/off control.
    _attr_assumed_state = True

    def __init__(self, bledomInstance: BLEDOMInstance, attr_name: str, entry_id: str) -> None:
        self._instance = bledomInstance
        self._attr_name = attr_name
        self._attr_unique_id = self._instance.address + "_mic_enable"
        # Disabled by default unless the model opts into mic support.
        self._attr_entity_registry_enabled_default = self._instance.model.get_supports_mic(self._instance.model_name)

    @property
    def is_on(self) -> bool:
        return self._instance.mic_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the microphone on."""
        await self._instance.enable_mic()
        LOG.debug(f"Microphone enabled for {self.name}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the microphone off."""
        await self._instance.disable_mic()
        LOG.debug(f"Microphone disabled for {self.name}")

    async def async_added_to_hass(self) -> None:
        """Subscribe to state pushes and best-effort restore the mic flag."""
        await super().async_added_to_hass()
        # If we were mic-on before a restart, seed the instance flag so the next
        # normal command still emits an explicit mic-off (harmless if the strip
        # was power-cycled out of music mode -- mic-off on a static strip is a
        # no-op).
        if (last_state := await self.async_get_last_state()) is not None and last_state.state == "on":
            self._instance._mic_enabled = True
            LOG.debug(f"Restored mic state for {self.name}: ON")
