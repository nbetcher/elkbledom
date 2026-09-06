"""Shared base entity for the elkbledom integration."""

from __future__ import annotations

from homeassistant.helpers import device_registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ElkCoordinator


class BLEDOMEntity(CoordinatorEntity[ElkCoordinator]):
    """Base entity providing shared DeviceInfo, availability and coordinator wiring.

    Every entity for one physical strip must report the same device identity and
    availability, so they are defined once here instead of being copy-pasted into
    each entity class (which let the device name drift depending on which platform
    registered last). Entities subclass this so these properties take MRO
    precedence.

    Phase 3/4 (docs/god-object-refactor.md §6): entities are now
    ``CoordinatorEntity``. The push that used to be wired per-entity via
    ``self._instance.register_callback(self.async_write_ha_state)`` is handled by
    the coordinator -- it subscribes to the transport callback registry once and
    re-publishes the optimistic ``ElkState`` snapshot, which
    ``CoordinatorEntity._handle_coordinator_update`` turns into an
    ``async_write_ha_state`` on every availability / mic-mode change. Entities read
    the composed stack through ``self._instance`` (facade) and ``self._device``
    (the ``ElkDevice`` intent surface) instead of poking privates.
    """

    # These entities are push-based (state is written after each command and on
    # advertisement/availability changes); HA must not schedule periodic polls.
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: ElkCoordinator) -> None:
        super().__init__(coordinator)
        # Convenience handles onto the composed stack (Phase 3): the facade for
        # read props / state, the device for intents.
        self._instance = coordinator.instance
        self._device = coordinator.device

    @property
    def available(self) -> bool:
        """Reachable per the strip's advertisements / recent successful commands.

        Driven by the bluetooth advertisement tracker and the write path (see
        BLEDOMInstance / BLETransport), so an unplugged or out-of-range strip is
        reported unavailable instead of latching online forever. Overrides
        CoordinatorEntity.available (which keys off last_update_success) because
        this coordinator never polls -- availability is the transport's truth.
        """
        return self.coordinator.last_update_success and self._instance.available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info (single source of truth for all entity types)."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._instance.address)},
            name=self._instance.config_name,
            connections={(device_registry.CONNECTION_BLUETOOTH, self._instance.address)},
            manufacturer="ElkBLEDOM",
            model=self._instance.model_name,
        )
