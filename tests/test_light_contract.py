"""Validate every model against Home Assistant's actual light contract."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.light import ColorMode, valid_supported_color_modes

from custom_components.elkbledom.light import BLEDOMLight
from custom_components.elkbledom.model import MODELS_DATA_KEY, Model


def test_every_shipped_model_has_valid_home_assistant_color_modes() -> None:
    directory = Path(__file__).parents[1] / "custom_components" / "elkbledom"
    raw_models = json.loads((directory / "models.json").read_text(encoding="utf-8"))
    models = {
        f"{item['name']}#{item['handle']}" if "handle" in item else item["name"]: item
        for item in raw_models
    }
    # Explicitly cover white-only and power-only future/unknown models too.
    models["white_only"] = {"commands": {"white": [1, "i"]}}
    models["power_only"] = {"commands": {"turn_on": [1], "turn_off": [0]}}
    manager = Model(SimpleNamespace(data={MODELS_DATA_KEY: models}))
    for key in models:
        instance = SimpleNamespace(model=manager, model_name=key, address="aa:bb:cc:dd:ee:ff")
        coordinator = SimpleNamespace(instance=instance, device=None)
        light = BLEDOMLight(coordinator, "entry")
        modes = valid_supported_color_modes(light.supported_color_modes)
        assert light._attr_color_mode in modes, key
        assert not (ColorMode.WHITE in modes and ColorMode.COLOR_TEMP in modes), key


async def test_reset_to_white_replaces_preserved_rgb_selection() -> None:
    light = BLEDOMLight.__new__(BLEDOMLight)
    light._instance = SimpleNamespace(is_on=False, reset=True)
    light._device = AsyncMock()
    light._attr_supported_color_modes = {ColorMode.RGB}
    await light._async_turn_on_locked()
    light._device.set_color.assert_awaited_once_with((255, 255, 255), is_base_color=True)
    light._device.set_brightness.assert_awaited_once_with(250)
    assert light._attr_color_mode == ColorMode.RGB


async def test_explicit_on_is_sent_when_optimistic_cache_already_says_on() -> None:
    light = BLEDOMLight.__new__(BLEDOMLight)
    light._instance = SimpleNamespace(is_on=True, reset=True)
    light._attr_color_mode = ColorMode.RGB
    light._device = AsyncMock()
    await light._async_turn_on_locked()
    light._device.turn_on.assert_awaited_once()
    # Resending power is not a reason to overwrite an existing color/effect.
    light._device.set_color.assert_not_awaited()
    light._device.set_brightness.assert_not_awaited()
