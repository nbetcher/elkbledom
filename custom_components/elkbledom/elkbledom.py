"""Thin ``BLEDOMInstance`` facade for the elkbledom integration.

The former ~1000-line god object is decomposed into cohesive layers:

* ``ElkState``     (state.py)     -- optimistic cache dataclass.
* ``ElkProtocol``  (protocol.py)  -- model indirection + model-independent frames
                                     (== the shared ``ModelContext``).
* ``BLETransport`` (transport.py) -- connection lifecycle, write path + pacing,
                                     ``@retry``, availability + callbacks, login,
                                     characteristic resolution, idle-disconnect.
* ``ElkDevice``    (device.py)    -- intents -> frames -> transport writes; holds
                                     state, brightness-mode + mic-exit policy.

``BLEDOMInstance`` composes that stack and re-exposes the ENTIRE public surface
the entities / ``__init__.py`` / ``config_flow.py`` / the ``ElkCoordinator``
depend on (see the compatibility contract in docs/god-object-refactor.md §4):
every method delegates to ``ElkDevice`` / ``BLETransport``; every property reads
through.

Phase 3/4 (docs/god-object-refactor.md §6): entities now reach the composed
layers through the public ``device`` (-> ``ElkDevice``), ``state``
(-> ``ElkState``), and ``transport`` (-> ``BLETransport``) accessors and call
``self._instance.state.restore(...)`` instead of poking privates, so the eight
externally-written ``_is_on``/``_brightness``/``_rgb_color``/``_rgb_color_base``/
``_color_temp_kelvin``/``_effect``/``_effect_speed``/``_mic_enabled`` shims are
gone.

``DeviceData`` now lives in ``device_data.py`` (to break the transport<->facade
import cycle, §2.5) and is re-exported here so ``config_flow.py:3``
(``from .elkbledom import DeviceData``) keeps resolving unchanged.
"""

import logging

from homeassistant.core import callback

# Re-exported so ``from .elkbledom import DeviceData`` (config_flow.py:3) keeps
# resolving after the class moved to device_data.py (§2.5 / §4.7). Do NOT remove.
from .device_data import DeviceData  # noqa: F401  (re-export)
from .state import ElkState
from .protocol import ElkProtocol
from .transport import BLETransport
from .device import ElkDevice

LOGGER = logging.getLogger(__name__)


class BLEDOMInstance:
    """Thin facade composing BLETransport + ElkProtocol + ElkState + ElkDevice.

    The public surface (constructor signature + defaults, read properties, async
    intents, the two ``@callback`` shims, and the eight externally-written private
    attributes) is frozen by the compatibility contract in
    docs/god-object-refactor.md §4. Nothing outside this module touches transport
    internals directly.
    """

    def __init__(self, address, reset: bool, delay: int, hass, forced_model: str = None,
                 brightness_mode: str = "auto", config_name: str = None) -> None:
        # Signature IDENTICAL to the god object (elkbledom.py:140). Two positional
        # call sites: __init__.py:120 (7 args) and config_flow.py:265 (5 args,
        # relying on the brightness_mode="auto" / config_name=None defaults).
        self._address = address
        self._reset = reset
        self._delay = delay
        self._hass = hass
        self._forced_model = forced_model
        # User-given name from the config entry; used as the single source for
        # every entity's DeviceInfo name so the device is labeled consistently.
        self._config_name = config_name

        # Build the stack (order matters -- §8.1): protocol -> state -> transport
        # -> device, all BEFORE any external code assigns the 8 private attrs.
        #
        # device_name_getter uses getattr (not attribute access) so an early call
        # -- or a future reorder -- degrades to None instead of raising, because
        # _transport is assigned two lines later (LOW-7). It is only actually
        # invoked at detect_model() below, after _transport exists.
        self._protocol = ElkProtocol(
            hass,
            device_name_getter=lambda: getattr(getattr(self, "_transport", None), "name", None),
            forced_model=forced_model,
        )
        self._state = ElkState()
        # Grabs asyncio.get_running_loop() + runs the discovery scan; raises
        # ConfigEntryNotReady when no connectable device is found (§4.1).
        self._transport = BLETransport(address, hass, delay, protocol=self._protocol)
        # Eager, synchronous pre-connect model resolution (god object elkbledom.py:214)
        # so entities can probe capabilities on model/model_name at their own
        # __init__, before any connect (§4.1 / §4.4).
        self._protocol.detect_model()
        self._device = ElkDevice(self._transport, self._protocol, self._state, brightness_mode)

        LOGGER.debug(
            'Model information for device %s : ModelNo %s, Turn on cmd %s, Turn off cmd %s, rssi %s',
            self.name, self.model_name,
            self.model.get_turn_on_cmd(self.model_name),
            self.model.get_turn_off_cmd(self.model_name),
            self.rssi,
        )

    # ------------------------------------------------------------------ #
    # Read properties: straight delegation (§4.2)                        #
    # ------------------------------------------------------------------ #
    @property
    def address(self):
        return self._address

    @property
    def reset(self):
        return self._reset

    @property
    def delay(self):
        return self._delay

    @property
    def forced_model(self):
        return self._forced_model

    @property
    def config_name(self):
        """User-given device name from the config entry.

        Falls back to the BLE advertised name if not provided (e.g. the
        transient instance the config flow builds during validation).
        """
        return self._config_name or self.name

    @property
    def name(self):
        return self._transport.name

    @property
    def available(self) -> bool:
        return self._transport.available

    @property
    def rssi(self):
        return self._transport.rssi

    @property
    def brightness_mode(self):
        return self._device.brightness_mode

    @property
    def model(self):
        return self._protocol.model

    @property
    def model_name(self):
        return self._protocol.model_name

    @property
    def min_color_temp_kelvin(self):
        return self._device.min_color_temp_kelvin

    @property
    def max_color_temp_kelvin(self):
        return self._device.max_color_temp_kelvin

    # ---- public read getters backed by ElkState (§4.2) ----
    @property
    def is_on(self):
        return self._state.is_on

    @property
    def rgb_color(self):
        return self._state.rgb_color

    @property
    def brightness(self):
        return self._state.brightness

    @property
    def color_temp_kelvin(self):
        return self._state.color_temp_kelvin

    @property
    def effect(self):
        return self._state.effect

    @property
    def effect_speed(self):
        return self._state.effect_speed

    @property
    def mic_effect(self):
        return self._state.mic_effect

    @property
    def mic_sensitivity(self):
        return self._state.mic_sensitivity

    @property
    def mic_enabled(self):
        return self._state.mic_enabled

    # ------------------------------------------------------------------ #
    # Public layer accessors (Phase 3/4): entities + the ElkCoordinator  #
    # talk to the composed stack directly instead of poking privates.    #
    # ------------------------------------------------------------------ #
    @property
    def device(self):
        """The ElkDevice (intent surface) entities call for set_color/etc."""
        return self._device

    @property
    def state(self):
        """The shared ElkState snapshot the coordinator pushes to entities."""
        return self._state

    @property
    def transport(self):
        """The BLETransport (connection/availability/callback registry)."""
        return self._transport

    # ------------------------------------------------------------------ #
    # Async intents: straight delegation to ElkDevice (§4.3)             #
    # ------------------------------------------------------------------ #
    async def apply_brightness_mode(self, mode: str) -> None:
        return await self._device.apply_brightness_mode(mode)

    async def set_color_temp(self, value: int) -> None:
        return await self._device.set_color_temp(value)

    async def set_color_temp_kelvin(self, value: int, brightness: int) -> None:
        return await self._device.set_color_temp_kelvin(value, brightness)

    async def set_color(self, rgb, is_base_color: bool = False) -> None:
        return await self._device.set_color(rgb, is_base_color)

    async def set_white(self, intensity: int) -> None:
        return await self._device.set_white(intensity)

    async def set_brightness(self, intensity: int) -> None:
        return await self._device.set_brightness(intensity)

    async def set_effect_speed(self, value: int) -> None:
        return await self._device.set_effect_speed(value)

    async def set_effect(self, value: int) -> None:
        return await self._device.set_effect(value)

    async def set_mic_effect(self, value: int) -> None:
        return await self._device.set_mic_effect(value)

    async def set_mic_sensitivity(self, value: int) -> None:
        return await self._device.set_mic_sensitivity(value)

    async def enable_mic(self) -> None:
        return await self._device.enable_mic()

    async def disable_mic(self) -> None:
        return await self._device.disable_mic()

    async def turn_on(self) -> None:
        return await self._device.turn_on()

    async def turn_off(self) -> None:
        return await self._device.turn_off()

    async def set_scheduler_on(self, days: int, hours: int, minutes: int, enabled: bool) -> None:
        return await self._device.set_scheduler_on(days, hours, minutes, enabled)

    async def set_scheduler_off(self, days: int, hours: int, minutes: int, enabled: bool) -> None:
        return await self._device.set_scheduler_off(days, hours, minutes, enabled)

    async def sync_time(self) -> None:
        return await self._device.sync_time()

    async def custom_time(self, hour: int, minute: int, second: int, day_of_week: int) -> None:
        return await self._device.custom_time(hour, minute, second, day_of_week)

    async def update(self) -> None:
        return await self._device.update()

    async def query_state(self) -> None:
        return await self._device.query_state()

    def get_color_base(self):
        return self._device.get_color_base()

    async def stop(self) -> None:
        return await self._transport.stop()

    # ------------------------------------------------------------------ #
    # Callback registry + the two HA @callback shims (§4.6)              #
    # ------------------------------------------------------------------ #
    def register_callback(self, callback_fn):
        """Register an entity state-update callback; returns an unregister fn."""
        return self._transport.register_callback(callback_fn)

    @callback
    def _async_update_ble(self, service_info, change) -> None:
        # __init__.py:129 captures this bound @callback at setup for teardown;
        # keep it a real @callback method on the facade (not a lazy delegate).
        self._transport._async_update_ble(service_info, change)

    @callback
    def _async_unavailable(self, service_info) -> None:
        self._transport._async_unavailable(service_info)
