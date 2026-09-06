"""Discovery/advertisement wrapper for the elkbledom integration.

``DeviceData`` wraps a single BLE discovery record for the config-flow
"is this supported?" filter: it resolves supportedness from the advertised
name (via ``ElkProtocol.is_supported``), exposes address/name/rssi, and can
refresh its RSSI from the latest advertisement.

Lives in its own module (not ``elkbledom.py``) to break the
``transport.py`` <-> ``elkbledom.py`` import cycle: transport's ctor constructs
``DeviceData`` during its discovery scan, while ``config_flow.py`` imports
``DeviceData`` from ``elkbledom`` -- if the class stayed in the facade module,
transport importing it from a partially-initialized ``elkbledom`` would raise
ImportError at load. ``elkbledom.py`` re-exports the class so ``config_flow.py:3``
(``from .elkbledom import DeviceData``) keeps resolving.

Phase 2 slim (docs/god-object-refactor.md §6): supportedness now routes through
``ElkProtocol.is_supported`` (single source of truth); the redundant second
frozen ``BLEDevice`` capture (transport already owns the connectable device) and
the stale ``_start_update`` "Parsing Govee" log helper were dropped. rssi lives
on ``BLETransport``; this stays only for the config-flow discovery filter.
"""

from homeassistant.components.bluetooth import async_discovered_service_info

from .protocol import ElkProtocol


class DeviceData:
    def __init__(self, hass, discovery_info):
        self._discovery = discovery_info
        self._supported = ElkProtocol.is_supported(hass, self._discovery.name)
        self._address = self._discovery.address
        self._name = self._discovery.name
        self._rssi = self._discovery.rssi
        self._hass = hass

    @property
    def is_supported(self) -> bool:
        return self._supported

    @property
    def address(self):
        return self._address

    @property
    def get_device_name(self):
        return self._name

    @property
    def name(self):
        return self._name

    @property
    def rssi(self):
        return self._rssi

    def update_device(self) -> None:
        for discovery_info in async_discovered_service_info(self._hass, connectable=True):
            if discovery_info.address.casefold() == self._address.casefold():
                self._rssi = discovery_info.rssi
