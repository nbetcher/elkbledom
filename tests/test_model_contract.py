"""Regression tests for the data-driven protocol contract."""

from __future__ import annotations

import asyncio
import json
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

from custom_components.elkbledom.const import EFFECTS_LIST_MAP, EFFECTS_list
from custom_components.elkbledom.model import MODELS_DATA_KEY, Model, ensure_models_loaded

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / "elkbledom"


def _models() -> list[dict]:
    return json.loads((INTEGRATION_DIR / "models.json").read_text(encoding="utf-8"))


def _model_manager() -> Model:
    models = {}
    for item in _models():
        key = f"{item['name']}#{item['handle']}" if "handle" in item else item["name"]
        models[key] = item
    return Model(SimpleNamespace(data={MODELS_DATA_KEY: models}))


def test_supported_model_keys_never_regress() -> None:
    names = {item["name"] for item in _models()}
    assert {"ELK-BLEDOM0E", "XSL-", "LED LIGHT STRIP"}.issubset(names)
    assert len(_models()) == 24


def test_every_supported_name_has_a_discovery_matcher() -> None:
    manifest = json.loads((INTEGRATION_DIR / "manifest.json").read_text(encoding="utf-8"))
    patterns = [matcher["local_name"] for matcher in manifest["bluetooth"]]
    for name in {item["name"] for item in _models()}:
        assert any(fnmatchcase(name, pattern) for pattern in patterns), name


def test_app_captured_elk_effect_frames() -> None:
    expected_effect = [126, 5, 3, "v", 3, 255, 255, 0, 239]
    expected_speed = [126, 4, 2, "v", 255, 255, 255, 0, 239]
    elk_names = {
        "ELK-BTC",
        "ELK-BLEDOB",
        "ELK-BLEDDM",
        "ELK-BLEDOM0E",
        "ELK-BLEDOM",
        "ELK-BLE",
    }
    matched = 0
    for item in _models():
        if item["name"] not in elk_names:
            continue
        matched += 1
        assert item["commands"]["effect"] == expected_effect
        assert item["commands"]["effect_speed"] == expected_speed
    assert matched == 7


def test_every_effect_list_member_has_an_encoder_value() -> None:
    definitions = json.loads((INTEGRATION_DIR / "definitions.json").read_text(encoding="utf-8"))
    for class_name, effect_values in definitions["effects_definitions"].items():
        suffix = class_name.removeprefix("EFFECTS")
        list_name = f"EFFECTS_list{suffix}"
        assert list_name in definitions["effects_lists"]
        assert set(definitions["effects_lists"][list_name]).issubset(effect_values)


def test_time_frame_kelvin_and_microphone_contract() -> None:
    model = _model_manager()
    assert model.get_sync_time_cmd("ELK-BLEDOM", 1, 2, 3, 0) == [
        0x7E,
        0x07,
        0x83,
        1,
        2,
        3,
        0,
        0xFF,
        0xEF,
    ]
    assert model.get_max_color_temp_kelvin("DMRRBA-007") == 40000
    assert model.get_supports_mic("MELK") is True


async def test_concurrent_model_load_cannot_publish_partial_effect_data() -> None:
    definitions_started = asyncio.Event()
    release_definitions = asyncio.Event()

    async def executor_job(function):
        if function.__name__ == "_load_definitions_json":
            definitions_started.set()
            await release_definitions.wait()
        return await asyncio.to_thread(function)

    hass = SimpleNamespace(data={}, async_add_executor_job=executor_job)
    first = asyncio.create_task(ensure_models_loaded(hass))
    await definitions_started.wait()
    assert MODELS_DATA_KEY not in hass.data
    second = asyncio.create_task(ensure_models_loaded(hass))
    await asyncio.sleep(0)
    assert not second.done()
    release_definitions.set()
    await asyncio.gather(first, second)
    assert EFFECTS_list
    assert EFFECTS_list == EFFECTS_LIST_MAP["EFFECTS_list"]
