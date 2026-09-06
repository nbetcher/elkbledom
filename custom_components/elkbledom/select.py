from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import BRIGHTNESS_MODES, CONF_BRIGHTNESS_MODE, MIC_EFFECTS, MIC_EFFECTS_list
from .coordinator import ElkCoordinator
from .entity import BLEDOMEntity
from .transport import UnsupportedCommandError

LOG = logging.getLogger(__name__)
PARALLEL_UPDATES = 0  # BLETransport serializes intents for each physical device.


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data.coordinator
    async_add_entities(
        [
            BLEDOMMicEffect(coordinator),
            BLEDOMBrightnessModeSelect(
                coordinator,
                config_entry,
            ),
        ]
    )


class BLEDOMMicEffect(BLEDOMEntity, RestoreEntity, SelectEntity):
    """Microphone Effect selector entity"""

    _attr_translation_key = "mic_effect"

    def __init__(self, coordinator: ElkCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = self._instance.address + "_mic_effect"
        self._current_option = MIC_EFFECTS_list[0]
        # Disabled by default unless the model opts into mic support.
        self._attr_entity_registry_enabled_default = self._instance.model.get_supports_mic(
            self._instance.model_name
        )

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @property
    def options(self) -> list[str]:
        # ELK/DOM strips only support 4 EQ modes (0x80-0x83); only MELK/MODELX
        # expose the full 8. Showing the extra four on a DOM strip is misleading
        # because the device clamps them all to the last valid mode.
        name = (self._instance.name or "").lower()
        if name.startswith(("melk", "modelx")):
            return MIC_EFFECTS_list
        return MIC_EFFECTS_list[:4]

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option not in self.options:
            raise UnsupportedCommandError(f"Invalid microphone effect: {option}")
        effect_value = MIC_EFFECTS[option].value
        await self._device.set_mic_effect(effect_value)
        self._current_option = option
        self.async_write_ha_state()
        LOG.debug("Mic effect set to %s (0x%02x)", option, effect_value)

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()

        # Restore the last known mic effect (only if still valid for this model)
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state in self.options:
                self._current_option = last_state.state
                LOG.debug(f"Restored mic effect for {self.name}: {self._current_option}")
            else:
                LOG.debug(f"Could not restore mic effect for {self.name}, using default")


class BLEDOMBrightnessModeSelect(BLEDOMEntity, SelectEntity):
    """Brightness Mode selector entity"""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "brightness_mode"

    def __init__(self, coordinator: ElkCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = self._instance.address + "_brightness_mode"

    @property
    def current_option(self) -> str | None:
        # Config entry options seed the running device and remain authoritative
        # across restarts; recorder state must not override a newer option.
        return self._instance.brightness_mode

    @property
    def options(self) -> list[str]:
        return BRIGHTNESS_MODES

    async def async_select_option(self, option: str) -> None:
        """Change the selected brightness mode."""
        if option not in BRIGHTNESS_MODES:
            raise UnsupportedCommandError(f"Invalid brightness mode: {option}")

        if option == self.current_option:
            return  # Nothing to change

        LOG.info("Changing brightness mode to %s for %s", option, self._instance.address)
        await self._device.apply_brightness_mode(option)

        # Update HA entry options
        data = dict(self._entry.options)
        data[CONF_BRIGHTNESS_MODE] = option
        self.hass.config_entries.async_update_entry(self._entry, options=data)

        self.async_write_ha_state()
