"""Diagnostics must use runtime_data and never expose device identifiers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.elkbledom.diagnostics import async_get_config_entry_diagnostics


@pytest.mark.parametrize("loaded", [False, True])
async def test_diagnostics_redacts_identifiers_with_or_without_runtime(loaded) -> None:
    address = "aa:bb:cc:dd:ee:ff"
    entry = SimpleNamespace(
        title="Private room",
        unique_id=address,
        version=1,
        minor_version=2,
        data={"mac": address, "name": "Private strip", "model": "ELK-BLEDOM"},
        options={"mac": address, "name": "Private option", "delay": 20},
    )
    if loaded:
        entry.runtime_data = SimpleNamespace(
            instance=SimpleNamespace(
                address=address,
                model_name="ELK-BLEDOM",
                transport=SimpleNamespace(is_connected=True),
                available=True,
                is_on=True,
                rssi=-60,
                brightness=128,
                rgb_color=(1, 2, 3),
                color_temp_kelvin=4000,
                effect=None,
                effect_speed=50,
                brightness_mode="rgb",
            ),
            coordinator=SimpleNamespace(
                name=f"elkbledom_{address.upper()}",
                last_update_success=True,
                update_interval=None,
            ),
        )

    result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)
    serialized = json.dumps(result).lower()
    assert address not in serialized
    assert "private" not in serialized
    assert result["entry"]["data"]["model"] == "ELK-BLEDOM"
    if loaded:
        assert result["instance"]["connected"] is True
    else:
        assert "instance" not in result
