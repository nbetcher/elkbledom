"""Regression tests for model capabilities and byte-safe command encoding."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from custom_components.elkbledom.model import MODELS_DATA_KEY, Model

INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / "elkbledom"


def _model_rows() -> list[dict]:
    return json.loads((INTEGRATION_DIR / "models.json").read_text(encoding="utf-8"))


def _model_manager() -> Model:
    models = {}
    for item in _model_rows():
        key = f"{item['name']}#{item['handle']}" if "handle" in item else item["name"]
        models[key] = item
    return Model(SimpleNamespace(data={MODELS_DATA_KEY: models}))


def test_adjustable_capabilities_require_placeholders_and_distinct_byte_frames() -> None:
    """A captured literal must not be advertised as an adjustable capability."""
    model = _model_manager()
    cases = (
        ("white", {"i"}, (1,), (255,)),
        ("brightness", {"i"}, (1,), (255,)),
        ("color_temp", {"w", "c"}, (0, 100), (100, 0)),
        ("color", {"r", "g", "b"}, (1, 2, 3), (4, 5, 6)),
        ("effect", {"v"}, (1,), (2,)),
        ("effect_speed", {"v"}, (0,), (100,)),
    )

    for key, model_data in model._models.items():
        commands = model_data.get("commands", {})
        for command_name, placeholders, first_args, second_args in cases:
            template = commands.get(command_name, [])
            getter = getattr(model, f"get_{command_name}_cmd")
            first = getter(key, *first_args)
            second = getter(key, *second_args)
            is_adjustable = placeholders.issubset(template)

            if not is_adjustable:
                assert first is None, (key, command_name, template)
                assert second is None, (key, command_name, template)
                continue

            assert first is not None, (key, command_name, template)
            assert second is not None, (key, command_name, template)
            assert first != second, (key, command_name, template)
            bytes(first)
            bytes(second)


def test_static_model_frames_are_byte_encodable() -> None:
    model = _model_manager()

    for key in model._models:
        turn_on = model.get_turn_on_cmd(key)
        turn_off = model.get_turn_off_cmd(key)
        assert turn_on is not None, key
        assert turn_off is not None, key
        bytes(turn_on)
        bytes(turn_off)
        if query := model.get_query_cmd(key):
            bytes(query)


def test_fixed_capture_models_retain_power_but_expose_no_adjustable_commands() -> None:
    model = _model_manager()

    for key in ("XSL-", "LED LIGHT STRIP"):
        bytes(model.get_turn_on_cmd(key))
        bytes(model.get_turn_off_cmd(key))
        assert model.get_white_cmd(key, 255) is None
        assert model.get_color_temp_cmd(key, 0, 100) is None
        assert model.get_effect_cmd(key, 135) is None
        assert model.get_effect_speed_cmd(key, 50) is None


def test_every_effect_exposed_by_a_variable_template_is_byte_encodable() -> None:
    model = _model_manager()
    definitions = json.loads((INTEGRATION_DIR / "definitions.json").read_text(encoding="utf-8"))
    effect_definitions = definitions["effects_definitions"]
    effect_lists = definitions["effects_lists"]

    for key, model_data in model._models.items():
        effect_class = model_data.get("effects_class", "EFFECTS")
        effect_list = model_data.get("effects_list", "EFFECTS_list")
        template = model_data.get("commands", {}).get("effect", [])
        for effect_name in effect_lists[effect_list]:
            value = effect_definitions[effect_class][effect_name]
            command = model.get_effect_cmd(key, value)
            if "v" not in template or not 0 <= value <= 255:
                assert command is None, (key, effect_name, value)
                continue
            assert command is not None, (key, effect_name, value)
            bytes(command)


def test_multibyte_melk_effect_ids_are_rejected_before_transport() -> None:
    model = _model_manager()
    unsupported = {
        "music_flow_flash": 384,
        "music_flash": 385,
        "music_rainbow": 386,
        "music_snake": 387,
        "music_rainbow_2": 388,
        "music_pulse": 389,
        "music_flow": 390,
        "music_pulse_2": 391,
    }

    for value in unsupported.values():
        assert model.get_effect_cmd("MELK-OA21", value) is None

    for invalid in (-1, 256, True, 1.5, "1"):
        assert model.get_effect_cmd("MELK-OA21", invalid) is None
