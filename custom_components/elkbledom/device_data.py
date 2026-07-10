"""Discovery/advertisement wrapper for the elkbledom integration.

``DeviceData`` wraps a single BLE discovery record: it resolves the model from
the advertised name (for the config-flow "is this supported?" check), exposes
address/name/rssi, and can refresh its RSSI from the latest advertisement.

Moved verbatim out of ``elkbledom.py`` (elkbledom.py:92-137) into its own module
to break the ``transport.py`` <-> ``elkbledom.py`` import cycle: transport's ctor
constructs ``DeviceData`` during its discovery scan, while ``config_flow.py``
imports ``DeviceData`` from ``elkbledom`` -- if the class stayed in the facade
module, transport importing it from a partially-initialized ``elkbledom`` would
raise ImportError at load. ``elkbledom.py`` re-exports the class so
``config_flow.py:3`` (``from .elkbledom import DeviceData``) keeps resolving.
Body untouched (see docs/god-object-refactor.md §2.5 / §8.9).
"""

import logging

from bleak.backends.device import BLEDevice
from home_assistant_bluetooth import BluetoothServiceInfo
from homeassistant.components.bluetooth import (
    async_discovered_service_info,
    async_ble_device_from_address,
)

from .model import Model

LOGGER = logging.getLogger(__name__)


class DeviceData():
    def __init__(self, hass, discovery_info):
        self._discovery = discovery_info
        model_manager = Model(hass)
        detected_model = model_manager.detect_model(self._discovery.name or "")
        self._supported = detected_model is not None
        self._address = self._discovery.address
        self._name = self._discovery.name
        self._rssi = self._discovery.rssi
        self._hass = hass
        self._bledevice = async_ble_device_from_address(hass, self._address)

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

    def bledevice(self) -> BLEDevice:
        return self._bledevice

    def update_device(self) -> None:
        #TODO for discovery_info in async_last_service_info(self._hass, self._address):
        for discovery_info in async_discovered_service_info(self._hass):
            if discovery_info.address == self._address:
                self._rssi = discovery_info.rssi
                ##TODO SOMETHING WITH DEVICE discovery_info
        return

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        LOGGER.debug("Parsing Govee BLE advertisement data: %s", service_info)
