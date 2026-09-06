"""Diagnostics support for ElkBLEDOM."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

REDACT_KEYS = {"mac", "name"}
_MAC_PATTERN = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])")


def _redact(value: str) -> str:
    if not value:
        return value
    return "***"


def _redact_macs(value: str) -> str:
    """Redact Bluetooth MAC addresses embedded in diagnostic text."""
    return _MAC_PATTERN.sub(lambda match: _redact(match.group(0)), value)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = getattr(entry, "runtime_data", None)
    instance = runtime.instance if runtime is not None else None
    coordinator = runtime.coordinator if runtime is not None else None

    data: dict[str, Any] = {
        "entry": {
            "title": _redact(entry.title),
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": {
                k: (_redact(str(v)) if k in REDACT_KEYS else v) for k, v in entry.data.items()
            },
            "options": {
                k: (_redact(str(v)) if k in REDACT_KEYS else v) for k, v in entry.options.items()
            },
            "unique_id": _redact(entry.unique_id or ""),
        },
    }

    if instance is not None:
        data["instance"] = {
            "address": _redact(instance.address),
            "model_name": instance.model_name,
            "connected": instance.transport.is_connected,
            "available": instance.available,
            "is_on": instance.is_on,
            "rssi": instance.rssi,
            "brightness": instance.brightness,
            "rgb_color": instance.rgb_color,
            "color_temp_kelvin": instance.color_temp_kelvin,
            "effect": instance.effect,
            "effect_speed": instance.effect_speed,
            "brightness_mode": instance.brightness_mode,
        }

    if coordinator is not None:
        coordinator_name = coordinator.name
        if instance is not None and instance.address:
            coordinator_name = coordinator_name.replace(instance.address, _redact(instance.address))
        data["coordinator"] = {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval)
            if coordinator.update_interval
            else None,
            "name": _redact_macs(coordinator_name),
        }

    return data
