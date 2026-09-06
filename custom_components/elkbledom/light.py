from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_WHITE,
    EFFECT_OFF,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import (
    CONF_EFFECTS_CLASS,
    EFFECTS,
    EFFECTS_LIST_MAP,
    EFFECTS_MAP,
    EFFECTS_list,
    effects_list_name_for_class,
)
from .coordinator import ElkCoordinator
from .entity import BLEDOMEntity
from .transport import UnsupportedCommandError

PARALLEL_UPDATES = 0  # BLETransport serializes complete actions per physical device.

LOGGER = logging.getLogger(__name__)


@dataclass
class BLEDOMLightExtraStoredData(ExtraStoredData):
    """Remember native settings even when HA hides attributes while off."""

    attributes: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"version": 1, **self.attributes}


async def async_setup_entry(hass, config_entry, async_add_devices) -> None:
    coordinator = config_entry.runtime_data.coordinator
    async_add_devices([BLEDOMLight(coordinator, config_entry.entry_id)])


class BLEDOMLight(BLEDOMEntity, RestoreEntity, LightEntity):
    # The strip has no state readback, so reported state is optimistic. Declaring
    # assumed_state makes the UI show explicit on/off buttons instead of a toggle
    # that implies confirmed truth (correct for an IR-remote/power-cut-desyncable
    # device).
    _attr_assumed_state = True

    def __init__(self, coordinator: ElkCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        has_white = bool(self._instance.model.get_white_cmd(self._instance.model_name, 255))
        has_color_temp = bool(
            self._instance.model.get_color_temp_cmd(self._instance.model_name, 50, 50)
        )
        has_rgb = bool(self._instance.model.get_color_cmd(self._instance.model_name, 255, 255, 255))
        has_effect = bool(self._instance.model.get_effect_cmd(self._instance.model_name, 1))
        self._has_effect_speed = self._instance.model.supports_effect_speed(
            self._instance.model_name
        )
        device_color_modes = set()
        if has_white and has_rgb and not has_color_temp:
            # HA WHITE is an extra mode for a color light, not a dimmable
            # white-only light. COLOR_TEMP and WHITE must not be combined.
            device_color_modes.add(ColorMode.WHITE)
        if has_color_temp:
            device_color_modes.add(ColorMode.COLOR_TEMP)
            self._attr_color_mode = ColorMode.COLOR_TEMP
        if has_rgb:
            device_color_modes.add(ColorMode.RGB)
            self._attr_color_mode = ColorMode.RGB
        if not device_color_modes:
            mode = ColorMode.BRIGHTNESS if has_white else ColorMode.ONOFF
            device_color_modes.add(mode)
            self._attr_color_mode = mode
        self._attr_supported_color_modes = device_color_modes
        self._attr_supported_features = (
            LightEntityFeature.EFFECT if has_effect else LightEntityFeature(0)
        )
        self._attr_name = None
        self._attr_effect = None
        self._attr_unique_id = self._instance.address
        self._hass = None

    @property
    def brightness(self):
        return self._instance.brightness

    @property
    def is_on(self) -> bool | None:
        return self._instance.is_on

    @property
    def color_temp_kelvin(self):
        return self._instance.color_temp_kelvin

    @property
    def max_color_temp_kelvin(self):
        return self._instance.max_color_temp_kelvin

    @property
    def min_color_temp_kelvin(self):
        return self._instance.min_color_temp_kelvin

    @property
    def effect_list(self):
        """Return list of available effects for this model."""
        if not self._attr_supported_features & LightEntityFeature.EFFECT:
            return None
        effects_class_name = self._get_configured_effects_class()
        effects_list_name = (
            effects_list_name_for_class(effects_class_name)
            if effects_class_name
            else self._instance.model.get_effects_list(self._instance.model_name)
        )
        effects_class = self._effect_enum()
        return [
            EFFECT_OFF,
            *(
                name
                for name in EFFECTS_LIST_MAP.get(effects_list_name, EFFECTS_list)
                if name in effects_class.__members__
                and self._instance.model.get_effect_cmd(
                    self._instance.model_name, effects_class[name].value
                )
            ),
        ]

    def _effect_enum(self):
        """Resolve the selected model/override's effect mapping consistently."""
        class_name = self._get_configured_effects_class() or self._instance.model.get_effects_class(
            self._instance.model_name
        )
        return EFFECTS_MAP.get(class_name) or EFFECTS_MAP.get("EFFECTS", EFFECTS)

    @property
    def effect(self):
        """Return current effect."""
        effect_value = self._instance.effect
        if effect_value is None:
            return EFFECT_OFF if self._attr_supported_features & LightEntityFeature.EFFECT else None
        effects_class_name = self._get_configured_effects_class()
        if not effects_class_name:
            effects_class_name = self._instance.model.get_effects_class(self._instance.model_name)
        effects_class = EFFECTS_MAP.get(effects_class_name) or EFFECTS_MAP.get("EFFECTS", EFFECTS)
        return next(
            (
                name
                for name, member in getattr(effects_class, "__members__", {}).items()
                if member.value == effect_value
            ),
            self._attr_effect,
        )

    @property
    def color_mode(self):
        """Only expose adjustments that the running effect can support."""
        if self._instance.effect is not None:
            if self._instance.brightness_mode != "rgb" and self._instance.model.get_brightness_cmd(
                self._instance.model_name, 255
            ):
                return ColorMode.BRIGHTNESS
            return ColorMode.ONOFF
        return self._attr_color_mode

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        if self._has_effect_speed:
            return {"effect_speed": self._instance.effect_speed}
        return {}

    @property
    def rgb_color(self):
        if self._instance.rgb_color is not None:
            # Dimming rounds the transmitted channels. Report the preserved
            # selection so HA's color picker and restored hue do not drift.
            # RGB has its own intensity, independent of HA's brightness value.
            # Expanding a dim base tuple to 255 changes the saved selection.
            return self._instance.get_color_base()
        return None

    @property
    def extra_restore_state_data(self) -> BLEDOMLightExtraStoredData:
        return BLEDOMLightExtraStoredData(
            {
                "color_mode": self._attr_color_mode,
                ATTR_BRIGHTNESS: self.brightness,
                ATTR_RGB_COLOR: self._instance.get_color_base(),
                ATTR_COLOR_TEMP_KELVIN: self.color_temp_kelvin,
                ATTR_EFFECT: self.effect,
            }
        )

    @property
    def should_poll(self) -> bool:
        """No polling needed for a demo light."""
        return False

    def _get_configured_effects_class(self) -> str | None:
        """Get the configured effects class from config entry options."""
        if not self._hass:
            return None

        config_entry = self._hass.config_entries.async_get_entry(self._entry_id)
        if config_entry:
            return config_entry.options.get(
                CONF_EFFECTS_CLASS, config_entry.data.get(CONF_EFFECTS_CLASS)
            )

        return None

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to hass."""
        await super().async_added_to_hass()

        # Store hass reference for config access
        self._hass = self.hass

        # Restore the last known state
        if (last_state := await self.async_get_last_state()) is not None:
            LOGGER.debug(f"Restoring previous state for {self.name}: {last_state.state}")
            attributes = dict(last_state.attributes)
            if (extra := await self.async_get_last_extra_data()) is not None:
                native = extra.as_dict()
                if isinstance(native, dict) and native.get("version") == 1:
                    attributes.update(native)
                    # Native data retains both selections even when the active
                    # mode is white, an effect, or the displayed state is off.
                    self._instance.state.restore(
                        rgb_color=native.get(ATTR_RGB_COLOR),
                        color_temp_kelvin=native.get(ATTR_COLOR_TEMP_KELVIN),
                    )
            restored_mode = attributes.get("color_mode")
            if not isinstance(restored_mode, str):
                restored_mode = None

            # Restore on/off state
            if last_state.state == "on":
                self._instance.state.restore(is_on=True)
                LOGGER.debug("Restored state: ON")
            elif last_state.state == "off":
                self._instance.state.restore(is_on=False)
                LOGGER.debug("Restored state: OFF")
            elif last_state.state == "unavailable":
                # If previous state was unavailable, assume device is off but available
                # This prevents the entity from staying unavailable after restart
                self._instance.state.restore(is_on=False)
                LOGGER.debug("Previous state was unavailable, setting to OFF")

            # Restore brightness (guard against a saved None, which would
            # overwrite the 255 default and leak None into reported state)
            restored_brightness = attributes.get(ATTR_BRIGHTNESS)
            if restored_brightness is not None:
                self._instance.state.restore(brightness=restored_brightness)
                LOGGER.debug(f"Restored brightness: {self._instance.state.brightness}")

            # HA also emits derived RGB attributes for a COLOR_TEMP light. Only
            # restore the native mode, not whichever attribute happens to exist.
            if attributes.get(ATTR_RGB_COLOR) is not None and (
                restored_mode == ColorMode.RGB
                or (restored_mode is None and attributes.get(ATTR_COLOR_TEMP_KELVIN) is None)
            ):
                try:
                    restored_rgb = tuple(attributes[ATTR_RGB_COLOR])
                    # couple_base=True also restores the unscaled base color (HA's
                    # rgb_color is independent of brightness); without this the first
                    # brightness change after a restart scales the default white
                    # base and the restored color is lost (strip goes white).
                    self._instance.state.restore(rgb_color=restored_rgb, couple_base=True)
                    if ColorMode.RGB in self._attr_supported_color_modes:
                        self._attr_color_mode = ColorMode.RGB
                        LOGGER.debug(f"Restored RGB color: {self._instance.state.rgb_color}")
                    else:
                        LOGGER.debug("Ignoring restored RGB mode unsupported by this model")
                except (TypeError, ValueError) as e:
                    LOGGER.warning(f"Invalid RGB color data, skipping: {e}")

            # Restore color temperature
            elif attributes.get(ATTR_COLOR_TEMP_KELVIN) is not None and restored_mode in (
                ColorMode.COLOR_TEMP,
                None,
            ):
                try:
                    self._instance.state.restore(
                        color_temp_kelvin=attributes[ATTR_COLOR_TEMP_KELVIN]
                    )
                    if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
                        self._attr_color_mode = ColorMode.COLOR_TEMP
                        LOGGER.debug(
                            f"Restored color temp: {self._instance.state.color_temp_kelvin}K"
                        )
                    else:
                        LOGGER.debug(
                            "COLOR_TEMP restored but model does not support it, ignoring color_mode"
                        )
                except (TypeError, ValueError) as e:
                    LOGGER.warning(f"Invalid color temperature data, skipping: {e}")

            # Restore white mode — only if model actually supports WHITE
            elif restored_mode == ColorMode.WHITE:
                if ColorMode.WHITE in self._attr_supported_color_modes:
                    self._attr_color_mode = ColorMode.WHITE
                    LOGGER.debug("Restored color mode: WHITE")
                else:
                    LOGGER.debug("Ignoring restored white mode unsupported by this model")

            # Restore effect
            if ATTR_EFFECT in attributes:
                self._attr_effect = attributes[ATTR_EFFECT]
                # Get the correct effects class (user configured or model default)
                effects_class_name = self._get_configured_effects_class()
                if not effects_class_name:
                    effects_class_name = self._instance.model.get_effects_class(
                        self._instance.model_name
                    )
                effects_class = EFFECTS_MAP.get(effects_class_name) or EFFECTS_MAP.get(
                    "EFFECTS", EFFECTS
                )
                if (
                    isinstance(self._attr_effect, str)
                    and self._attr_effect in effects_class.__members__
                    and self._attr_effect in (self.effect_list or ())
                ):
                    self._instance.state.restore(effect=effects_class[self._attr_effect].value)
                else:
                    self._attr_effect = None
                    self._instance.state.restore(effect=None)
                LOGGER.debug(f"Restored effect: {self._attr_effect}")

            # Restore effect speed from extra attributes
            if "effect_speed" in last_state.attributes:
                try:
                    self._device.restore_effect_speed(last_state.attributes["effect_speed"])
                    LOGGER.debug(f"Restored effect speed: {self._instance.state.effect_speed}")
                except (TypeError, ValueError) as e:
                    LOGGER.warning(f"Invalid effect speed data, using default: {e}")
        else:
            # No previous state found, set default values
            LOGGER.debug(f"No previous state found for {self.name}, setting defaults")
            self._instance.state.restore(is_on=False, brightness=255)

        # Final safety: ensure color_mode is always valid for this model
        if self._attr_color_mode not in self._attr_supported_color_modes:
            fallback = next(iter(self._attr_supported_color_modes), ColorMode.ONOFF)
            LOGGER.warning(
                "%s: color_mode '%s' not in supported %s after restore, resetting to '%s'",
                self.name,
                self._attr_color_mode,
                self._attr_supported_color_modes,
                fallback,
            )
            self._attr_color_mode = fallback

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Apply a complete light action without cross-platform interleaving."""
        async with self._instance.batch():
            await self._async_turn_on_locked(**kwargs)
        self.async_write_ha_state()

    async def _async_turn_on_locked(self, **kwargs: Any) -> None:
        LOGGER.debug(f"Params turn on: {kwargs} color mode: {self._attr_color_mode}")
        # Validate before power/mode writes, including direct entity callers.
        if ATTR_EFFECT in kwargs and kwargs[ATTR_EFFECT] not in (self.effect_list or ()):
            raise UnsupportedCommandError(f"Unsupported effect: {kwargs[ATTR_EFFECT]!r}")
        was_on = self.is_on
        # Assumed state can be stale after an IR command or a power cycle.
        # An explicit ON must reach the strip even when the cache already says ON.
        await self._device.turn_on()
        if not was_on and self._instance.reset:
            LOGGER.debug(
                "Change color to white to reset led strip when other infrared control interact"
            )
            if ColorMode.RGB in self._attr_supported_color_modes:
                # Reset is a new static selection, not a dimmed copy of the
                # previous hue. Keep the preserved/reportable base in sync.
                await self._device.set_color((255, 255, 255), is_base_color=True)
                await self._device.set_brightness(250)
                self._attr_color_mode = ColorMode.RGB
            elif self._attr_supported_color_modes & {ColorMode.WHITE, ColorMode.BRIGHTNESS}:
                await self._device.set_white(250)
            elif ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
                midpoint = (
                    self._instance.min_color_temp_kelvin + self._instance.max_color_temp_kelvin
                ) // 2
                await self._device.set_color_temp_kelvin(midpoint, 250)
            self._attr_effect = None
            # ATTR_WHITE (if present) is applied by the White block below.

        brightness = kwargs.get(ATTR_BRIGHTNESS)
        # Whether a color/temp/white write below already applied the requested
        # brightness, so we never send a redundant second brightness command.
        brightness_handled = False

        # --- Color temperature ---
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            new_temp = kwargs[ATTR_COLOR_TEMP_KELVIN]
            new_brightness = brightness if brightness is not None else self.brightness
            await self._device.set_color_temp_kelvin(new_temp, new_brightness)
            self._attr_effect = None
            self._attr_color_mode = ColorMode.COLOR_TEMP
            brightness_handled = True

        # --- White ---
        if ATTR_WHITE in kwargs:
            if ColorMode.WHITE not in self._attr_supported_color_modes:
                raise UnsupportedCommandError(f"{self.name}: white mode is not supported")
            await self._device.set_white(kwargs[ATTR_WHITE])
            self._attr_color_mode = ColorMode.WHITE
            self._attr_effect = None
            brightness_handled = True

        # --- RGB color: write the color, then brightness exactly once ---
        if ATTR_RGB_COLOR in kwargs:
            color = tuple(kwargs[ATTR_RGB_COLOR])
            target_brightness = brightness if brightness is not None else self.brightness
            # Always write the color: cached state is not a reliable proxy for
            # what the strip physically shows (mode switches, running effects,
            # IR-remote changes), so an explicit rgb_color request is always sent.
            await self._device.set_color(color, is_base_color=True)
            if target_brightness is not None:
                # Apply the complete requested selection, including 255. An
                # optimistic cached 255 cannot prove that the independent
                # native register was not dimmed by the IR remote. RGB-only
                # models also apply the same preserved base/brightness pair.
                await self._device.set_brightness(target_brightness)
            self._attr_color_mode = ColorMode.RGB
            self._attr_effect = None
            brightness_handled = True

        # --- Brightness only (no color/temp/white attribute in this call) ---
        if brightness is not None and not brightness_handled:
            if self._instance.effect is not None:
                await self._device.set_brightness(brightness)
            elif self._attr_color_mode == ColorMode.COLOR_TEMP:
                current_temp = self.color_temp_kelvin
                if not current_temp:
                    # No temp known yet — use midpoint of model range
                    current_temp = (
                        self._instance.min_color_temp_kelvin + self._instance.max_color_temp_kelvin
                    ) // 2
                await self._device.set_color_temp_kelvin(current_temp, brightness)
                self._attr_effect = None
            elif self._attr_color_mode in (ColorMode.WHITE, ColorMode.BRIGHTNESS):
                await self._device.set_white(brightness)
            else:  # RGB or any other mode
                await self._device.set_brightness(brightness)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs[ATTR_EFFECT]
            if effect_name == EFFECT_OFF:
                if self._attr_color_mode == ColorMode.COLOR_TEMP:
                    await self._device.set_color_temp_kelvin(
                        self.color_temp_kelvin or self.min_color_temp_kelvin, self.brightness
                    )
                elif self._attr_color_mode in (ColorMode.WHITE, ColorMode.BRIGHTNESS):
                    await self._device.set_white(self.brightness)
                else:
                    await self._device.set_color(
                        self._instance.get_color_base(), is_base_color=True
                    )
                    await self._device.set_brightness(self.brightness)
                self._attr_effect = None
                return
            # Get the correct effects class (user configured or model default)
            effects_class_name = self._get_configured_effects_class()
            if not effects_class_name:
                effects_class_name = self._instance.model.get_effects_class(
                    self._instance.model_name
                )
            effects_class = EFFECTS_MAP.get(effects_class_name) or EFFECTS_MAP.get(
                "EFFECTS", EFFECTS
            )
            if effect_name not in effects_class.__members__:
                raise UnsupportedCommandError(
                    f"Effect {effect_name!r} is not available for {effects_class_name}"
                )
            effect_value = effects_class[effect_name].value
            await self._device.set_effect(effect_value)
            # Also send effect speed to ensure it's applied
            if self._has_effect_speed and self._instance.effect_speed is not None:
                await self._device.set_effect_speed(self._instance.effect_speed)
            self._attr_effect = effect_name

    async def async_turn_off(self, **kwargs: Any) -> None:
        LOGGER.debug(f"Params turn off: {kwargs} color mode: {self._attr_color_mode}")
        await self._device.turn_off()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        await self._device.update()
        self.async_write_ha_state()
