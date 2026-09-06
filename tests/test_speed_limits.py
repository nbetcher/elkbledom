"""One speed contract across model encoding, actions, entities and recorder data."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.number import NumberExtraStoredData, RestoreNumber

from custom_components.elkbledom.device import ElkDevice
from custom_components.elkbledom.entity import BLEDOMEntity
from custom_components.elkbledom.light import BLEDOMLight
from custom_components.elkbledom.limits import EffectSpeedLimits
from custom_components.elkbledom.model import MODELS_DATA_KEY, Model
from custom_components.elkbledom.number import (
    BLEDOMEffectSpeed,
    BLEDOMMicSensitivity,
    async_setup_entry,
)
from custom_components.elkbledom.state import ElkState
from custom_components.elkbledom.transport import UnsupportedCommandError


@pytest.fixture
def model():
    path = Path(__file__).parents[1] / "custom_components/elkbledom/models.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    models = {
        f"{row['name']}#{row['handle']}" if "handle" in row else row["name"]: row for row in rows
    }
    return Model(SimpleNamespace(data={MODELS_DATA_KEY: models}))


def _stack(model, key="ELK-BLEDOB"):
    @asynccontextmanager
    async def operation():
        yield

    async def write(frame):
        if not frame:
            raise UnsupportedCommandError("No adjustable speed command")

    transport = SimpleNamespace(
        name=key,
        operation=operation,
        write_frame=AsyncMock(side_effect=write),
        fire_callbacks=Mock(),
    )
    state = ElkState()
    device = ElkDevice(transport, SimpleNamespace(model=model, model_name=key), state)

    class Instance:
        address = "aa:bb:cc:dd:ee:ff"
        model_name = key

        @property
        def effect_speed(self):
            return state.effect_speed

    instance = Instance()
    instance.model = model
    instance.state = state
    return SimpleNamespace(instance=instance, device=device)


def test_shipped_speed_limits_remain_conservative_and_fixed_frames_are_not_sliders(model):
    for key in model._models:
        assert model.get_effect_speed_limits(key) == EffectSpeedLimits(0, 100)
        if model.supports_effect_speed(key):
            assert model.get_effect_speed_cmd(key, 0) != model.get_effect_speed_cmd(key, 100)
    for key in ("XSL-", "LED LIGHT STRIP", "ELK-BTCW", "unknown"):
        assert not model.supports_effect_speed(key)
        assert model.get_effect_speed_cmd(key, 50) is None


@pytest.mark.parametrize("value", [0, 50, 100, 100.0])
async def test_valid_speed_is_encoded_without_rescaling_and_shared_with_number(model, value):
    coordinator = _stack(model)
    entity = BLEDOMEffectSpeed(coordinator)
    entity.async_write_ha_state = Mock()
    await entity.async_set_native_value(value)
    coordinator.device.transport.write_frame.assert_awaited_once_with(
        [126, 4, 2, int(value), 255, 255, 255, 0, 239]
    )
    assert coordinator.instance.effect_speed == entity.native_value == value
    assert entity.extra_restore_state_data.native_value == value


@pytest.mark.parametrize("value", [-1, 101, 183, 255, 25.5, float("nan"), float("inf"), True])
async def test_invalid_speed_fails_before_io_and_does_not_publish_state(model, value):
    coordinator = _stack(model)
    entity = BLEDOMEffectSpeed(coordinator)
    entity.async_write_ha_state = Mock()
    with pytest.raises(UnsupportedCommandError):
        await entity.async_set_native_value(value)
    coordinator.device.transport.write_frame.assert_not_awaited()
    entity.async_write_ha_state.assert_not_called()
    assert entity.native_value == 50


async def test_a_future_evidenced_model_range_is_used_at_every_layer(model):
    # Synthetic metadata tests the extension point, not hardware support for 183.
    model._models["ELK-BLEDOB"]["effect_speed_range"] = {"min": 60, "max": 183}
    coordinator = _stack(model)
    entity = BLEDOMEffectSpeed(coordinator)
    assert entity.native_min_value == 60
    assert entity.native_max_value == 183
    assert entity.native_value == 60  # Existing default is constrained too.
    await coordinator.device.set_effect_speed(183)
    assert coordinator.instance.effect_speed == 183
    coordinator.device.transport.write_frame.assert_awaited_once_with(
        [126, 4, 2, 183, 255, 255, 255, 0, 239]
    )
    coordinator.device.restore_effect_speed(255, from_number=True)
    assert entity.native_value == 183
    coordinator.device.restore_effect_speed(50)  # Older light record cannot win.
    assert entity.native_value == 183


@pytest.mark.parametrize("limits", [{"min": -1}, {"max": 256}, {"min": 101}, {"max": True}, []])
def test_malformed_model_limits_fail_closed(model, limits):
    model._models["ELK-BLEDOB"]["effect_speed_range"] = limits
    with pytest.raises(ValueError):
        model.get_effect_speed_cmd("ELK-BLEDOB", 50)


@pytest.mark.parametrize("key", ["XSL-", "LED LIGHT STRIP"])
async def test_fixed_speed_model_has_no_slider_or_automatic_speed_write(model, key):
    coordinator = _stack(model, key)
    add_entities = Mock()
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    await async_setup_entry(SimpleNamespace(), entry, add_entities)
    assert not any(
        isinstance(entity, BLEDOMEffectSpeed) for entity in add_entities.call_args.args[0]
    )
    light = BLEDOMLight(coordinator, "entry")
    assert light.extra_state_attributes == {}
    assert light._has_effect_speed is False
    with pytest.raises(UnsupportedCommandError):
        await coordinator.device.set_effect_speed(50)
    assert coordinator.instance.effect_speed == 50


@pytest.mark.parametrize("number_first", [False, True])
def test_number_restore_wins_over_legacy_light_speed_regardless_of_order(model, number_first):
    coordinator = _stack(model)
    actions = [(20, False), (80, True)]
    if number_first:
        actions.reverse()
    for value, from_number in actions:
        coordinator.device.restore_effect_speed(value, from_number=from_number)
    assert coordinator.instance.effect_speed == 80
    coordinator.device.transport.write_frame.assert_not_awaited()


@pytest.mark.parametrize("entity_type", [BLEDOMEffectSpeed, BLEDOMMicSensitivity])
async def test_restore_number_uses_native_data_and_current_limits(model, monkeypatch, entity_type):
    monkeypatch.setattr(BLEDOMEntity, "async_added_to_hass", AsyncMock())
    coordinator = _stack(model)
    entity = entity_type(coordinator)
    assert isinstance(entity, RestoreNumber)
    entity._attr_translation_key = None
    entity._attr_name = "Test number"
    entity.async_get_last_extra_data = AsyncMock(
        return_value=NumberExtraStoredData(255, 0, 1, None, 183)
    )
    entity.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="10", attributes={}))
    await entity.async_added_to_hass()
    assert entity.native_value == 100
    assert entity.extra_restore_state_data.native_max_value == 100
    entity.async_get_last_state.assert_not_awaited()
    coordinator.device.transport.write_frame.assert_not_awaited()


@pytest.mark.parametrize("entity_type", [BLEDOMEffectSpeed, BLEDOMMicSensitivity])
@pytest.mark.parametrize("stored", ["74.0", "999", "unavailable", "NaN", "Infinity"])
async def test_legacy_unitless_number_restore_migrates_safely(
    model, monkeypatch, entity_type, stored
):
    monkeypatch.setattr(BLEDOMEntity, "async_added_to_hass", AsyncMock())
    coordinator = _stack(model)
    entity = entity_type(coordinator)
    entity.async_get_last_extra_data = AsyncMock(return_value=None)
    entity._attr_translation_key = None
    entity._attr_name = "Test number"
    entity.async_get_last_state = AsyncMock(
        return_value=SimpleNamespace(state=stored, attributes={})
    )
    await entity.async_added_to_hass()
    assert entity.native_value == {"74.0": 74, "999": 100}.get(stored, 50)
    coordinator.device.transport.write_frame.assert_not_awaited()


async def test_legacy_display_units_are_not_mistaken_for_native_speed(model, monkeypatch):
    monkeypatch.setattr(BLEDOMEntity, "async_added_to_hass", AsyncMock())
    entity = BLEDOMEffectSpeed(_stack(model))
    entity.async_get_last_extra_data = AsyncMock(return_value=None)
    entity.async_get_last_state = AsyncMock(
        return_value=SimpleNamespace(state="80", attributes={"unit_of_measurement": "%"})
    )
    await entity.async_added_to_hass()
    assert entity.native_value == 50


async def test_failed_speed_write_keeps_previous_value(model):
    coordinator = _stack(model)
    coordinator.device.transport.write_frame.side_effect = RuntimeError("injected failure")
    with pytest.raises(RuntimeError, match="injected failure"):
        await coordinator.device.set_effect_speed(80)
    assert coordinator.instance.effect_speed == 50
