"""Shared base entity for the elkbledom integration."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .elkbledom import BLEDOMInstance


class BLEDOMEntity:
    """Mixin providing shared DeviceInfo and availability for every elkbledom entity.

    Every entity for one physical strip must report the same device identity and
    availability, so they are defined once here instead of being copy-pasted into
    each entity class (which let the device name drift depending on which platform
    registered last). Entities mix this in first so these properties take MRO
    precedence.
    """

    _instance: "BLEDOMInstance"

    @property
    def available(self) -> bool:
        """No reliable state readback exists; available once is_on is known."""
        return self._instance.is_on is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info (single source of truth for all entity types)."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._instance.address)},
            name=self._instance.config_name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.address)},
        )
