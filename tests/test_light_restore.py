"""Round-trip HA-generated light states, including attributes hidden while off."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.light import ColorMode
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.restore_state import RestoredExtraData, StoredState, async_get

from custom_components.elkbledom.device import ElkDevice
from custom_components.elkbledom.elkbledom import BLEDOMInstance
from custom_components.elkbledom.light import BLEDOMLight
from custom_components.elkbledom.model import ensure_models_loaded
from custom_components.elkbledom.protocol import ElkProtocol
from custom_components.elkbledom.state import ElkState
from custom_components.elkbledom.transport import UnsupportedCommandError


@pytest.fixture
async def lights(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    await ensure_models_loaded(hass)

    def create(state=None, model="ELK-BLEDOM"):
        instance = BLEDOMInstance.__new__(BLEDOMInstance)
        instance._state = state or ElkState()
        instance._address = "aa:bb:cc:dd:ee:ff"
        instance._protocol = ElkProtocol(hass, lambda: model, forced_model=model)
        instance._protocol.detect_model()
        instance._transport = SimpleNamespace(name=model)
        instance._device = ElkDevice(instance._transport, instance._protocol, instance.state)
        coordinator = SimpleNamespace(
            instance=instance,
            device=instance.device,
            async_add_listener=lambda *_args: lambda: None,
        )
        entity = BLEDOMLight(coordinator, "entry")
        entity.hass = hass
        entity.entity_id = "light.review"
        entity.async_write_ha_state = Mock()
        return entity

    def save(entity, *, native=True):
        # Use HA's actual state attributes and JSON serialization, not a
        # hand-crafted dict that incorrectly leaves RGB/brightness present off.
        attributes = json.loads(json.dumps(entity.state_attributes))
        extra = (
            RestoredExtraData(json.loads(json.dumps(entity.extra_restore_state_data.as_dict())))
            if native
            else None
        )
        async_get(hass).last_states[entity.entity_id] = StoredState(
            State(entity.entity_id, "on" if entity.is_on else "off", attributes),
            extra,
            datetime.now(UTC),
        )
        return attributes

    yield create, save, hass
    await hass.async_stop(force=True)


async def test_legacy_color_temperature_ignores_ha_derived_rgb(lights):
    create, save, _hass = lights
    old = create(ElkState(is_on=True, brightness=90, color_temp_kelvin=3000))
    old._attr_color_mode = ColorMode.COLOR_TEMP
    attributes = save(old, native=False)
    assert attributes["rgb_color"] is not None  # HA supplies a derived approximation.
    new = create()
    await new.async_added_to_hass()
    assert new.color_mode == ColorMode.COLOR_TEMP
    assert new.color_temp_kelvin == 3000
    assert new.brightness == 90


@pytest.mark.parametrize("mode", [ColorMode.RGB, ColorMode.COLOR_TEMP])
@pytest.mark.parametrize("is_on", [False, True])
async def test_native_settings_survive_on_off_round_trip(lights, mode, is_on):
    create, save, _hass = lights
    rgb = (128, 64, 32)
    old = create(
        ElkState(
            is_on=is_on,
            brightness=90,
            rgb_color=(45, 22, 11),
            rgb_color_base=rgb,
            color_temp_kelvin=3000,
        )
    )
    old._attr_color_mode = mode
    attributes = save(old)
    if not is_on:
        assert attributes["brightness"] is attributes["rgb_color"] is None
    new = create()
    await new.async_added_to_hass()
    assert new.is_on is is_on
    assert new.color_mode == mode
    assert new.brightness == 90
    assert new.color_temp_kelvin == 3000
    assert new.rgb_color == rgb
    assert new._instance.get_color_base() == rgb


async def test_running_effect_is_remembered_while_off(lights):
    create, save, _hass = lights
    old = create(ElkState(is_on=False, brightness=80))
    name = next(name for name in old.effect_list if name != "off")
    value = old._effect_enum()[name].value
    old._instance.state.effect = value
    save(old)
    new = create()
    await new.async_added_to_hass()
    assert new.effect == name
    assert new._instance.effect == value
    assert new.is_on is False


async def test_unsupported_saved_mode_and_effect_are_not_reinstated(lights):
    create, save, _hass = lights
    old = create(ElkState(is_on=True, brightness=90, color_temp_kelvin=3000))
    name = next(name for name in old.effect_list if name != "off")
    old._instance.state.effect = old._effect_enum()[name].value
    old._attr_color_mode = ColorMode.COLOR_TEMP
    save(old)
    new = create(model="XSL-")
    await new.async_added_to_hass()
    assert new.color_mode == ColorMode.ONOFF
    assert new._instance.effect is None


async def test_unknown_extra_schema_falls_back_to_legacy_state(lights):
    create, save, hass = lights
    old = create(ElkState(is_on=True, brightness=90, color_temp_kelvin=3000))
    old._attr_color_mode = ColorMode.COLOR_TEMP
    save(old)
    stored = async_get(hass).last_states[old.entity_id]
    stored.extra_data = RestoredExtraData({"version": 99, "brightness": 255})
    new = create()
    await new.async_added_to_hass()
    assert new.brightness == 90
    assert new.color_temp_kelvin == 3000


async def test_malformed_native_settings_do_not_break_entity_restoration(lights):
    create, save, hass = lights
    old = create(ElkState(is_on=False))
    save(old)
    async_get(hass).last_states[old.entity_id].extra_data = RestoredExtraData(
        {
            "version": 1,
            "color_mode": [],
            "brightness": float("inf"),
            "rgb_color": [float("inf"), 0, 0],
            "color_temp_kelvin": float("inf"),
            "effect": [],
        }
    )
    new = create()
    await new.async_added_to_hass()
    assert new.brightness == 255
    assert new._instance.get_color_base() == (255, 255, 255)
    assert new._instance.effect is None


async def test_every_advertised_effect_is_encodable_and_melk_music_is_hidden(lights):
    create, _save, _hass = lights
    for key in create()._instance.model._models:
        light = create(model=key)
        for name in light.effect_list or ():
            if name != "off":
                bytes(light._instance.model.get_effect_cmd(key, light._effect_enum()[name].value))
    melk = create(model="MELK-OA21")
    assert not any(name.startswith("music_") for name in melk.effect_list)
    melk._device.turn_on = AsyncMock()
    with pytest.raises(UnsupportedCommandError):
        await melk._async_turn_on_locked(effect="music_flow_flash")
    melk._device.turn_on.assert_not_awaited()


@pytest.mark.parametrize("brightness", [None, 255])
async def test_rgb_selection_reapplies_full_brightness_despite_optimistic_cache(lights, brightness):
    create, _save, _hass = lights
    light = create(ElkState(is_on=True, brightness=255))
    light._device = AsyncMock()
    kwargs = {"rgb_color": (128, 64, 32)}
    if brightness is not None:
        kwargs["brightness"] = brightness
    await light._async_turn_on_locked(**kwargs)
    light._device.turn_on.assert_awaited_once()
    light._device.set_color.assert_awaited_once_with((128, 64, 32), is_base_color=True)
    light._device.set_brightness.assert_awaited_once_with(255)


@pytest.mark.parametrize("key", ["XSL-", "LED LIGHT STRIP"])
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("set_white", (50,)),
        ("set_color_temp_kelvin", (3000, 50)),
        ("set_color", ((128, 64, 32),)),
        ("set_brightness", (50,)),
        ("set_effect", (37,)),
    ],
)
async def test_unsupported_controls_do_not_exit_music_or_write(lights, key, method, args):
    create, _save, _hass = lights
    light = create(ElkState(mic_enabled=True), model=key)
    transport = light._device.transport

    @asynccontextmanager
    async def operation():
        yield

    transport.operation = operation
    transport.write_frame = AsyncMock()
    transport.write_frames = AsyncMock()
    transport.fire_callbacks = Mock()
    with pytest.raises(UnsupportedCommandError):
        await getattr(light._device, method)(*args)
    transport.write_frame.assert_not_awaited()
    transport.write_frames.assert_not_awaited()
    assert light._instance.state.mic_enabled is True
