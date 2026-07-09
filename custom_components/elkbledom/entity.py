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
    # These entities are push-based (state is written after each command and on
    # advertisement/availability changes); HA must not schedule periodic polls.
    _attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to instance state pushes (availability, mic mode)."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._instance.register_callback(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Reachable per the strip's advertisements / recent successful commands.

        Driven by the bluetooth advertisement tracker and the write path (see
        BLEDOMInstance), so an unplugged or out-of-range strip is reported
        unavailable instead of latching online forever.
        """
        return self._instance.available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info (single source of truth for all entity types)."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._instance.address)},
            name=self._instance.config_name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.address)},
        )
