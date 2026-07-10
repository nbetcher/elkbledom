import asyncio
import logging
from typing import Any, TypeVar, cast, Optional, List
from collections.abc import Callable

from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
)
from homeassistant.components.bluetooth import (
    async_discovered_service_info,
    async_ble_device_from_address,
    BluetoothServiceInfoBleak,
    BluetoothChange,
)
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady

from .device_data import DeviceData

LOGGER = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
#DISCONNECT_DELAY = 120
BLEAK_BACKOFF_TIME = 0.25
# Minimum spacing between consecutive BLE writes to one strip. These controllers
# silently drop a frame that arrives too soon after the previous one; the vendor
# Android app paces its multi-frame sequences ~450ms apart. We keep a small gap
# on every write (collision avoidance, barely perceptible) and add a longer
# settle specifically after a mode switch (see MIC_EXIT_SETTLE). Enforced as a
# min-gap-since-last-write so a single command after idle pays nothing.
COMMAND_GAP = 0.15
# Extra settle after the mic-power-off frame so the firmware finishes leaving
# music mode before the following color/effect frame lands. Used by ElkDevice
# (imported from here) so there is a single definition.
MIC_EXIT_SETTLE = 0.3
RETRY_BACKOFF_EXCEPTIONS = (BleakDBusError,)
WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

def retry_bluetooth_connection_error(func: WrapFuncType) -> WrapFuncType:
    """Define a wrapper to retry on bleak error.

    The accessory is allowed to disconnect us any time so
    we need to retry the operation.
    """

    async def _async_wrap_retry_bluetooth_connection_error(
        self: "ElkDevice", *args: Any, **kwargs: Any
    ) -> Any:
        # LOGGER.debug("%s: Starting retry loop", self.name)
        attempts = DEFAULT_ATTEMPTS
        max_attempts = attempts - 1

        for attempt in range(attempts):
            try:
                return await func(self, *args, **kwargs)
            except BleakNotFoundError:
                # The lock cannot be found so there is no
                # point in retrying.
                self._set_available(False)
                raise
            except RETRY_BACKOFF_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    self._set_available(False)
                    LOGGER.debug("%s: %s error calling %s, reach max attempts (%s/%s)",self.name,type(err),func,attempt,max_attempts,exc_info=True,)
                    raise
                LOGGER.debug("%s: %s error calling %s, backing off %ss, retrying (%s/%s)...",self.name,type(err),func,BLEAK_BACKOFF_TIME,attempt,max_attempts,exc_info=True,)
                await asyncio.sleep(BLEAK_BACKOFF_TIME)
            except BLEAK_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    self._set_available(False)
                    LOGGER.debug("%s: %s error calling %s, reach max attempts (%s/%s): %s",self.name,type(err),func,attempt,max_attempts,err,exc_info=True,)
                    raise
                LOGGER.debug("%s: %s error calling %s, backing off %ss, retrying  (%s/%s)...: %s",self.name,type(err),func,BLEAK_BACKOFF_TIME,attempt,max_attempts,err,exc_info=True,)
                await asyncio.sleep(BLEAK_BACKOFF_TIME)

    return cast(WrapFuncType, _async_wrap_retry_bluetooth_connection_error)

class CharacteristicMissingError(Exception):
    """Raised when a characteristic is missing."""

class BLETransport:
    """Owns the BLE connection lifecycle, the write path + pacing, the retry
    decorator, availability + the callback registry, MELK/MODELX login,
    characteristic resolution + notifications, the idle-disconnect machinery, and
    both locks. Model indirection / UUID lookups / handle refinement live in the
    injected ElkProtocol (the ONE upward seam, see docs/god-object-refactor.md
    §2.4)."""

    def __init__(self, address, hass, delay, *, protocol: "ElkProtocol") -> None:
        self.loop = asyncio.get_running_loop()
        self._address = address
        self._delay = delay
        self._hass = hass
        # Shared model/UUID holder; transport calls read_uuid()/write_uuid()/
        # refine_by_handle() + the connection-family predicates on it (§2.4).
        self._protocol = protocol
        self._device = None
        self._device_data: DeviceData | None = None
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        # Serializes whole write operations (connect + write) for THIS device so
        # concurrent commands can't interleave on the characteristic and the idle
        # disconnect can't tear down the client mid-write. It is per-instance, so
        # it never blocks commands to other devices.
        self._operation_lock: asyncio.Lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        # Monotonic timestamp (loop clock) of the last GATT write, used to pace
        # consecutive writes by COMMAND_GAP. None until the first write.
        self._last_write_at: float | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        # Strong reference to the in-flight idle-disconnect task (asyncio only
        # keeps a weak ref, so without this it can be GC'd mid-await).
        self._disconnect_task: asyncio.Task | None = None
        self._cached_services: BleakGATTServiceCollection | None = None
        self._expected_disconnect = False
        self._read_uuid = None
        self._write_uuid = None
        # Entity state-update callbacks (availability / mic state pushes) and the
        # advertisement-driven availability flag. Optimistic-True until the strip
        # is proven gone (stops advertising AND isn't connected, or a command
        # fails after retries). Latest RSSI is refreshed from advertisements.
        self._callbacks: List[Callable[[], None]] = []
        self._available = True
        self._rssi_latest: int | None = None

        try:
            # connectable=True: we need a link we can actually open (via a proxy or
            # a local adapter), not just an advertisement-only device.
            self._device = async_ble_device_from_address(hass, self._address, connectable=True)
        except (Exception) as error:
            LOGGER.error("Error getting device: %s", error)

        for discovery_info in async_discovered_service_info(hass):
            if discovery_info.address == address:
                devicedata = DeviceData(hass, discovery_info)
                #LOGGER.debug("device %s: %s %s",devicedata.name, devicedata.address, devicedata.rssi)
                if devicedata.is_supported:
                    self._device_data = devicedata

        if not self._device:
            raise ConfigEntryNotReady(f"You need to add bluetooth integration (https://www.home-assistant.io/integrations/bluetooth) or couldn't find a nearby device with address: {address}")

    # ---- identity / state (properties) ----
    @property
    def name(self):
        return self._device.name

    @property
    def address(self):
        return self._address

    @property
    def rssi(self):
        if self._rssi_latest is not None:
            return self._rssi_latest
        return 0 if self._device_data is None else self._device_data.rssi

    @property
    def available(self) -> bool:
        """Whether the strip is reachable (advertising, connected, or recently so)."""
        return self._available

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    # ---- availability / callbacks ----
    def register_callback(self, callback_fn: Callable[[], None]) -> Callable[[], None]:
        """Register an entity state-update callback; returns an unregister fn.

        Entities register ``self.async_write_ha_state`` so an availability change
        or a mic-mode transition pushes fresh state to HA without polling.
        """
        def unregister() -> None:
            if callback_fn in self._callbacks:
                self._callbacks.remove(callback_fn)
        self._callbacks.append(callback_fn)
        return unregister

    def fire_callbacks(self) -> None:
        """Notify every registered entity to refresh its state."""
        for cb in list(self._callbacks):
            cb()

    def set_available(self, available: bool) -> None:
        """Flip availability and push it to entities only on a real change."""
        if available == self._available:
            return
        self._available = available
        LOGGER.debug("%s: availability -> %s", self.name, "available" if available else "unavailable")
        self.fire_callbacks()

    @callback
    def _async_update_ble(self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange) -> None:
        """Refresh the connectable BLEDevice + RSSI from a live advertisement.

        Keeps ``self._device`` current so (re)connects target the present best
        path (the canonical HA BLE pattern), and treats a fresh advertisement as
        proof the strip is reachable.
        """
        self._device = service_info.device
        self._rssi_latest = service_info.rssi
        self.set_available(True)

    @callback
    def _async_unavailable(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Mark unavailable when the strip stops advertising.

        Guarded by the live connection: a strip we hold a link to may stop
        advertising, so the "gone" signal is ignored while connected. When the
        link later drops or a command fails after retries, availability flips
        through those paths instead (avoids flapping on a busy single radio).
        """
        if self._client and self._client.is_connected:
            return
        self.set_available(False)

    # ---- connection + writes (the whole ElkDevice -> BLE surface) ----
    async def connect(self) -> None:
        # Serialize connect as a whole operation (same lock every write takes) so
        # the idle-disconnect can't tear down mid-connect and a concurrent write
        # can't interleave.
        async with self._operation_lock:
            await self._ensure_connected()

    async def write_frame(self, data) -> None:
        """Send command to device and read response."""
        if not data:
            return
        # Serialize the connect+write as one unit so overlapping commands to the
        # same device can't interleave, and the idle-disconnect can't run between
        # _ensure_connected() and the actual write (which would null out _client).
        async with self._operation_lock:
            await self._ensure_connected()
            await self._write_while_connected(data)

    async def write_frames(self, *data_items) -> None:
        """Send several commands under a single lock acquisition so a paired
        sequence (e.g. color-temp + white) lands atomically and can't be
        interleaved by a competing command on the same device. Falsy items
        (empty/None commands) are skipped."""
        items = [d for d in data_items if d]
        if not items:
            return
        async with self._operation_lock:
            await self._ensure_connected()
            for data in items:
                await self._write_while_connected(data)

    def refresh_device_data(self) -> None:
        if self._device_data is not None:
            self._device_data.update_device()

    async def _write_while_connected(self, data) -> None:
        # Pace consecutive frames: these strips drop a write that arrives too
        # soon after the previous one. Only delays when commands are bunched
        # (a lone command after idle finds the gap already elapsed).
        if self._last_write_at is not None:
            gap = COMMAND_GAP - (self.loop.time() - self._last_write_at)
            if gap > 0:
                await asyncio.sleep(gap)
        LOGGER.debug(''.join(format(x, ' 03x') for x in data))
        await self._client.write_gatt_char(self._write_uuid, data, False)
        self._last_write_at = self.loop.time()
        # A successful write proves the strip is reachable even when it isn't
        # advertising (e.g. while we hold the connection).
        self.set_available(True)

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete; RSSI: %s",
                self.name,
                self.rssi,
            )
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return

            LOGGER.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)
            try:
                client = await establish_connection(
                        BleakClientWithServiceCache,
                        self._device,
                        self.name,
                        self._disconnected,
                        cached_services=self._cached_services,
                        # Re-query HA for the freshest connectable device each
                        # attempt (falling back to the last known one) so the
                        # retry connector can pick the current best proxy path
                        # instead of a device frozen at setup time.
                        ble_device_callback=lambda: (
                            async_ble_device_from_address(self._hass, self._address, connectable=True)
                            or self._device
                        ),
                        # One connect attempt here; the outer
                        # @retry_bluetooth_connection_error owns the retry budget
                        # so the two layers don't multiply into a long, radio-
                        # hogging storm under the operation lock.
                        max_attempts=1,
                    )
            except asyncio.TimeoutError:
                # Re-raise rather than return: returning leaves self._client None
                # and the caller's _write_while_connected then dereferences None
                # (uncaught AttributeError). Raising lets @retry handle/retry it.
                LOGGER.error("%s: Connection attempt timed out; RSSI: %s", self.name, self.rssi)
                raise

            LOGGER.debug("%s: Connected; RSSI: %s", self.name, self.rssi)

            # Execute login command BEFORE resolving characteristics for MELK/MODELX devices
            # These devices disconnect if login is not performed first
            if self._protocol.requires_login(self._device.name):
                LOGGER.debug("%s: Executing login procedure before service discovery; RSSI: %s", self.name, self.rssi)
                try:
                    # Get services to find write UUID for login
                    temp_services = None
                    try:
                        temp_services = client.services
                    except (AttributeError, Exception):
                        try:
                            temp_services = await client.get_services()
                        except (AttributeError, Exception) as e:
                            LOGGER.error("%s: Failed to get services: %s", self.name, e)
                            raise

                    temp_write_uuid = None

                    # Find write characteristic for login
                    write_uuid = self._protocol.write_uuid()
                    if write_uuid and (char := temp_services.get_characteristic(write_uuid)):
                        temp_write_uuid = str(char.uuid)  # Ensure it's a string
                        LOGGER.debug("%s: Found write UUID for login: %s", self.name, temp_write_uuid)

                    if temp_write_uuid:
                        LOGGER.info("%s: Executing login sequence...", self.name)
                        login_frame_one, login_frame_two = self._protocol.login_frames()
                        login_step_delay = self._protocol.login_step_delay()
                        await client.write_gatt_char(temp_write_uuid, login_frame_one, response=False)
                        await asyncio.sleep(login_step_delay)
                        await client.write_gatt_char(temp_write_uuid, login_frame_two, response=False)
                        await asyncio.sleep(login_step_delay)
                        LOGGER.info("%s: Login sequence completed", self.name)
                    else:
                        LOGGER.warning("%s: Could not find write UUID for login procedure", self.name)
                except Exception as e:
                    LOGGER.error("%s: Login procedure failed: %s", self.name, e)
                    # Continue anyway, might work for some devices

            # Try to get services with fallback
            services_obj = None
            try:
                services_obj = client.services
            except (AttributeError, Exception):
                try:
                    services_obj = await client.get_services()
                except (AttributeError, Exception) as e:
                    LOGGER.error("%s: Failed to get services: %s", self.name, e)
                    await client.disconnect()
                    raise

            resolved = self._resolve_characteristics(services_obj)
            if not resolved:
                # Try to handle services failing to load
                try:
                    # Try alternate method
                    alt_services = None
                    try:
                        alt_services = await client.get_services()
                    except (AttributeError, Exception):
                        try:
                            alt_services = client.services
                        except (AttributeError, Exception) as e:
                            LOGGER.warning("%s: Could not get services with either method: %s", self.name, e)
                            raise

                    if alt_services:
                        resolved = self._resolve_characteristics(alt_services)
                        self._cached_services = alt_services if resolved else None
                except (AttributeError, Exception) as error:
                    LOGGER.warning("%s: Could not resolve characteristics from services; RSSI: %s", self.name, self.rssi)
            else:
                self._cached_services = services_obj if resolved else None

            if not resolved:
                await client.clear_cache()
                await client.disconnect()
                raise CharacteristicMissingError(
                    "Failed to find supported characteristics, device may not be supported"
                )

            LOGGER.debug("%s: Characteristics resolved: %s; RSSI: %s", self.name, resolved, self.rssi)

            self._client = client
            self.set_available(True)
            self._reset_disconnect_timer()

            # Enable notifications (simple method, no manual CCCD)
            try:
                if self._protocol.uses_notifications(self._device.name):
                    if self._read_uuid is not None and isinstance(self._read_uuid, str) and self._read_uuid.lower() != "none":
                        LOGGER.debug("%s: Enabling notifications; RSSI: %s", self.name, self.rssi)
                        await client.start_notify(self._read_uuid, self._notification_handler)
                        LOGGER.info("%s: Notifications enabled", self.name)
                    else:
                        LOGGER.warning("%s: Read UUID not resolved (value: %s), skipping notifications", self.name, self._read_uuid)
            except Exception as e:
                LOGGER.warning("%s: Notifications could not be enabled: %s", self.name, e)

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification data from the device.

        These ELK-BLE* strips echo every command we write back on the notify
        characteristic; they do not emit reliable status frames. The echoed
        brightness/white command (7e 04 01 <i> ff 00 ff 00 ef) has 0x01 at
        byte[2], so the previous parser mistook it for a status reply and read
        bytes 4-6 (ff 00 ff) as RGB magenta and byte[7] (0x00) as 0% brightness
        -- corrupting state so every colour change reported magenta and dropped
        brightness to zero. The command methods are the source of truth for
        power/colour/brightness, so we only log here and never overwrite state
        from these echoes.
        """
        self._notification_received = True
        LOGGER.debug("%s: Notification (command echo) received (%d bytes): %s", self.name, len(data), ' '.join(f'{x:02x}' for x in data))
        return

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve characteristics."""
        if not services:
            LOGGER.debug("%s: No services provided to resolve characteristics, dont should works", self.name)

        # Log all available characteristics for debugging
        LOGGER.debug("%s: Available services and characteristics:", self.name)
        for service in services:
            LOGGER.debug("%s: Service %s", self.name, service.uuid)
            for char in service.characteristics:
                LOGGER.debug("%s:   Characteristic %s (properties: %s)", self.name, char.uuid, char.properties)

        # Try to find read characteristic
        read_uuid = self._protocol.read_uuid()
        if read_uuid and (char := services.get_characteristic(read_uuid)):
            self._read_uuid = str(char.uuid)  # Ensure it's a string
            LOGGER.debug("%s: Found read UUID: %s with handle %s", self.name, self._read_uuid, char.handle if hasattr(char, 'handle') else 'Unknown')
        else:
            self._read_uuid = None
            LOGGER.warning("%s: Could not find read characteristic: %s", self.name, read_uuid)

        # Try to find write characteristic
        write_uuid = self._protocol.write_uuid()
        if write_uuid and (char := services.get_characteristic(write_uuid)):
            self._write_uuid = str(char.uuid)  # Ensure it's a string
            char_handle = char.handle if hasattr(char, 'handle') else None
            LOGGER.debug("%s: Found write UUID: %s with handle %s", self.name, self._write_uuid, f"0x{char_handle:04x}" if char_handle else 'Unknown')

            # Re-detect model based on handle if available
            if char_handle is not None:
                self._protocol.refine_by_handle(char_handle)
                # Update write_uuid in case model changed
                write_uuid = self._protocol.write_uuid()
                if write_uuid:
                    self._write_uuid = str(write_uuid)
        else:
            self._write_uuid = None
            LOGGER.error("%s: Could not find write characteristic: %s", self.name, write_uuid)

        # For devices like MELK that don't use notifications, only write_uuid is required
        if not self._protocol.requires_read_uuid(self._device.name):
            result = bool(self._write_uuid)
            LOGGER.debug("%s: Device doesn't require read UUID, resolved: %s", self.name, result)
            return result

        return bool(self._read_uuid and self._write_uuid)

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        if self._delay is not None and self._delay != 0:
            LOGGER.debug("%s: Configured disconnect from device in %s seconds; RSSI: %s", self.name, self._delay, self.rssi)
            self._disconnect_timer = self.loop.call_later(
                self._delay, self._disconnect
            )

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._expected_disconnect:
            LOGGER.debug("%s: Disconnected from device; RSSI: %s", self.name, self.rssi)
            return
        LOGGER.warning("%s: Device unexpectedly disconnected; RSSI: %s",self.name,self.rssi,)

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        # Keep a strong reference (asyncio holds only a weak one) so the task
        # can't be garbage-collected mid-await, which would leak the connection.
        self._disconnect_task = asyncio.create_task(self._execute_timed_disconnect())

    async def stop(self) -> None:
        """Stop the strip and release its timers/tasks (called on unload)."""
        LOGGER.debug("%s: Stop", self.name)
        # Cancel the pending idle-disconnect timer so it can't fire after the
        # instance is torn down -- otherwise a live TimerHandle bound to
        # self._disconnect keeps the instance alive for up to `delay` seconds.
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        # If an idle-disconnect task is already in flight, AWAIT it to completion
        # rather than cancelling it: that task is running the same clean
        # client.disconnect() we want. Cancelling raised CancelledError (a
        # BaseException, so uncaught by the `except Exception` in
        # _execute_disconnect) INSIDE client.disconnect(), aborting the teardown;
        # the fallback below then no-oped because the task had already nulled
        # self._client -> the link was never cleanly closed. Awaiting also
        # prevents the task leak this cleanup was added for. Never self-await.
        task = self._disconnect_task
        self._disconnect_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            try:
                await task
            except Exception as e:
                LOGGER.debug("%s: idle-disconnect task during stop: %s", self.name, e)
        await self._execute_disconnect()

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            self._delay,
        )
        await self._execute_disconnect(timed=True)

    async def _execute_disconnect(self, timed: bool = False) -> None:
        """Execute disconnection."""
        # Use the operation lock (not just the connect lock) so a disconnect can
        # never run between _ensure_connected() and the write inside write_frame().
        async with self._operation_lock:
            # For idle (timed) disconnects: if a command refreshed the connection
            # while this was queued behind it on the lock, it re-armed the idle
            # timer -- skip the now-stale teardown so we don't kill a connection
            # that's actively in use (which would force an immediate reconnect).
            if timed and self._disconnect_timer is not None:
                LOGGER.debug("%s: Skipping stale idle disconnect; connection was refreshed", self.name)
                return
            read_char = self._read_uuid if hasattr(self, '_read_uuid') else None
            client = self._client
            LOGGER.debug("Disconnecting: READ_UUID=%s, CLIENT_CONNECTED=%s", read_char, client.is_connected if client else "No Client")
            self._expected_disconnect = True
            self._client = None
            self._write_uuid = None
            self._read_uuid = None
            if client and client.is_connected:
                try:
                    if read_char and self._protocol.uses_notifications(self._device.name):
                        await client.stop_notify(read_char)
                    await client.disconnect()
                except Exception as e:
                    LOGGER.error("Error during disconnection: %s", e)
