"""Push-only ``DataUpdateCoordinator`` for the elkbledom integration.

These strips have **no readback**: ``BLETransport._notification_handler`` only
logs the command echoes and ``ElkDevice.update()`` seeds state without reading
it back (docs/god-object-refactor.md §6 Phase 4). So this coordinator does NOT
poll -- ``update_interval=None`` and ``_async_update_data`` merely returns the
optimistic :class:`~.state.ElkState` snapshot, introducing zero BLE traffic.

Instead it orchestrates availability + optimistic-state push: it subscribes to
the transport's callback registry (the same registry the availability flip and
mic-mode transitions fire) and re-publishes the ``ElkState`` snapshot to entities
on every such change. Entities become ``CoordinatorEntity`` and are refreshed via
the coordinator rather than each holding its own ``register_callback``
subscription.
"""

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .elkbledom import BLEDOMInstance
from .state import ElkState

LOGGER = logging.getLogger(__name__)


class ElkCoordinator(DataUpdateCoordinator[ElkState]):
    """Availability + optimistic-state push coordinator (no polling)."""

    def __init__(self, hass: HomeAssistant, instance: BLEDOMInstance) -> None:
        # update_interval=None => the coordinator NEVER schedules a refresh, so no
        # periodic connect/poll is ever introduced (invariant: no new BLE traffic).
        super().__init__(
            hass,
            LOGGER,
            name=instance.address,
            update_interval=None,
        )
        self.instance = instance
        self.device = instance.device
        # Re-publish the optimistic snapshot whenever the transport fires its
        # callbacks (availability change / mic-mode transition). Returns an
        # unregister fn, torn down in async_shutdown().
        self._unsub = instance.register_callback(self._handle_push)

    @callback
    def _handle_push(self) -> None:
        """Push the current ElkState snapshot to entities on a transport event."""
        self.async_set_updated_data(self.instance.state)

    async def _async_update_data(self) -> ElkState:
        """Return the optimistic snapshot; introduces NO BLE traffic (no connect)."""
        return self.instance.state

    async def async_shutdown(self) -> None:
        """Unsubscribe from the transport and stop the underlying instance."""
        await super().async_shutdown()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await self.instance.stop()
