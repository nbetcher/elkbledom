"""Unit tests for proxy-safe BLE operations and optimistic state semantics."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.exc import BleakError

import custom_components.elkbledom.transport as transport_module
from custom_components.elkbledom.device import ElkDevice
from custom_components.elkbledom.state import ElkState
from custom_components.elkbledom.transport import (
    BLETransport,
    NotConnectedError,
    UnsupportedCommandError,
)


class FakeModel:
    """Minimal model surface for intent tests."""

    @staticmethod
    def get_color_cmd(_name, red, green, blue):
        return [0x7E, 0, 5, 3, red, green, blue, 0, 0xEF]

    @staticmethod
    def get_query_cmd(_name):
        return None


class FakeProtocol:
    model = FakeModel()
    model_name = "ELK-BLEDOM"

    @staticmethod
    def requires_login(_name):
        return False

    @staticmethod
    def uses_notifications(_name):
        return False


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.is_connected = True
        self.fail = fail
        self.writes: list[bytes] = []
        self.responses: list[bool] = []
        self.active = 0
        self.max_active = 0
        self.write_times: list[float] = []

    async def write_gatt_char(self, _uuid, data, *, response: bool) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.write_times.append(asyncio.get_running_loop().time())
        try:
            await asyncio.sleep(0.005)
            if self.fail:
                raise BleakError("injected write failure")
            self.responses.append(response)
            self.writes.append(bytes(data))
        finally:
            self.active -= 1

    async def disconnect(self) -> None:
        self.is_connected = False


def _transport(client: FakeClient) -> BLETransport:
    transport = BLETransport.__new__(BLETransport)
    transport.loop = asyncio.get_running_loop()
    transport._address = "AA:BB:CC:DD:EE:FF"
    transport._delay = 0
    transport._protocol = FakeProtocol()
    transport._device = SimpleNamespace(name="ELK-BLEDOM", address="AA:BB:CC:DD:EE:FF")
    transport._device_data = None
    transport._connect_lock = asyncio.Lock()
    transport._operation_lock = asyncio.Lock()
    transport._operation_owner = None
    transport._operation_depth = 0
    transport._batch_depth = 0
    transport._client = client
    transport._stopped = False
    transport._notifications_enabled = False
    transport._last_write_at = None
    transport._disconnect_timer = None
    transport._disconnect_task = None
    transport._cached_services = None
    transport._expected_disconnect = False
    transport._read_uuid = None
    transport._write_uuid = "write"
    transport._write_requires_response = True
    transport._callbacks = []
    transport._available = True
    transport._advertisement_available = True
    transport._rssi_latest = None
    transport._ensure_connected = AsyncMock()
    return transport


async def test_cross_entity_writes_are_serialized_paced_and_use_gatt_mode(monkeypatch) -> None:
    monkeypatch.setattr(transport_module, "COMMAND_GAP", 0.01)
    client = FakeClient()
    transport = _transport(client)
    device = ElkDevice(transport, FakeProtocol(), ElkState())

    await asyncio.gather(
        device.set_color((1, 2, 3), is_base_color=True),
        device.set_color((4, 5, 6), is_base_color=True),
    )

    assert client.max_active == 1
    assert client.responses == [True, True]
    assert client.write_times[1] - client.write_times[0] >= 0.009


async def test_failed_write_retries_without_publishing_state(monkeypatch) -> None:
    monkeypatch.setattr(transport_module, "BLEAK_BACKOFF_TIME", 0)
    monkeypatch.setattr(transport_module, "COMMAND_GAP", 0)
    client = FakeClient(fail=True)
    state = ElkState(rgb_color=(9, 9, 9))
    device = ElkDevice(_transport(client), FakeProtocol(), state)

    with pytest.raises(NotConnectedError):
        await device.set_color((10, 20, 30), is_base_color=True)

    assert state.rgb_color == (9, 9, 9)
    assert len(client.write_times) == 3


async def test_missing_model_command_fails_without_connecting_or_mutating() -> None:
    client = FakeClient()
    protocol = FakeProtocol()
    protocol.model = SimpleNamespace(get_color_cmd=lambda *_args: None)
    state = ElkState(rgb_color=(9, 9, 9))
    transport = _transport(client)
    device = ElkDevice(transport, protocol, state)

    with pytest.raises(UnsupportedCommandError):
        await device.set_color((10, 20, 30), is_base_color=True)

    transport._ensure_connected.assert_not_awaited()
    assert state.rgb_color == (9, 9, 9)


async def test_connector_uses_one_attempt_and_fresh_proxy_path(monkeypatch) -> None:
    stale_device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="ELK-BLEDOM")
    fresh_device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="ELK-BLEDOM")
    client = SimpleNamespace(is_connected=True, services=object())
    captured = {}

    async def establish(*_args, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(transport_module, "establish_connection", establish)
    transport = _transport(FakeClient())
    transport._client = None
    transport._device = stale_device
    transport._fresh_ble_device = lambda: fresh_device
    transport._resolve_characteristics = lambda _services: True
    transport._write_uuid = None
    transport._ensure_connected = BLETransport._ensure_connected.__get__(transport)

    await transport._ensure_connected()

    assert captured["max_attempts"] == 1
    assert captured["ble_device_callback"]() is fresh_device
    assert transport._device is fresh_device


def test_restore_clamps_values_without_device_io() -> None:
    state = ElkState()
    state.restore(
        is_on=True,
        brightness=300,
        effect_speed=255,
        rgb_color=(-1, 100, 999),
        color_temp_kelvin=99999,
    )

    assert state.is_on is True
    assert state.brightness == 255
    assert state.effect_speed == 100
    assert state.rgb_color == (0, 100, 255)
    assert state.color_temp_kelvin == 40000


async def test_unavailable_advertisement_is_applied_when_live_connection_drops() -> None:
    client = FakeClient()
    transport = _transport(client)
    service_info = SimpleNamespace()

    transport._async_unavailable(service_info)
    assert transport.available is True

    client.is_connected = False
    transport._disconnected(client)
    assert transport.available is False


async def test_stop_releases_proxy_slot_when_stop_notify_fails() -> None:
    client = FakeClient()
    client.stop_notify = AsyncMock(side_effect=BleakError("proxy lost notification handle"))
    transport = _transport(client)
    transport._notifications_enabled = True
    transport._read_uuid = "read"

    await transport.stop()

    client.stop_notify.assert_awaited_once_with("read")
    assert client.is_connected is False
    assert transport._client is None


async def test_stop_awaits_an_in_flight_idle_disconnect() -> None:
    client = FakeClient()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def disconnect():
        entered.set()
        await finish.wait()
        client.is_connected = False

    client.disconnect = disconnect
    transport = _transport(client)
    transport._disconnect()
    await entered.wait()
    stop = asyncio.create_task(transport.stop())
    await asyncio.sleep(0)
    assert client.is_connected is True
    finish.set()
    await asyncio.wait_for(stop, 1)
    assert client.is_connected is False
    assert transport._disconnect_task is None


async def test_unloaded_transport_cannot_reconnect() -> None:
    transport = _transport(FakeClient())
    await transport.stop()
    transport._ensure_connected = BLETransport._ensure_connected.__get__(transport)
    with pytest.raises(NotConnectedError, match="unloaded"):
        await transport.write_frame([1])


async def test_failed_idle_disconnect_retains_usable_client_and_retries_cleanup() -> None:
    client = FakeClient()
    disconnect = client.disconnect
    client.disconnect = AsyncMock(side_effect=BleakError("temporary proxy failure"))
    transport = _transport(client)
    transport._delay = 20

    await transport._execute_timed_disconnect()

    assert transport._client is client
    assert transport._write_uuid == "write"
    assert transport._disconnect_timer is not None
    await transport.write_frame([1])
    assert client.writes == [b"\x01"]

    client.disconnect = disconnect
    await transport.stop()
    assert transport._client is None
    assert transport._disconnect_timer is None


async def test_cancelled_melk_login_releases_proxy_slot(monkeypatch) -> None:
    client = FakeClient()
    characteristic = SimpleNamespace(uuid="write", properties=["write"])
    client.services = SimpleNamespace(get_characteristic=lambda _uuid: characteristic)
    started = asyncio.Event()

    async def write(*_args, **_kwargs):
        started.set()

    client.write_gatt_char = write
    transport = _transport(client)
    transport._client = None
    transport._protocol = SimpleNamespace(
        requires_login=lambda _name: True,
        write_uuid=lambda: "write",
        login_frames=lambda: (b"one", b"two"),
        login_step_delay=lambda: 60,
    )
    transport._fresh_ble_device = lambda: transport._device
    transport._ensure_connected = BLETransport._ensure_connected.__get__(transport)
    monkeypatch.setattr(transport_module, "establish_connection", AsyncMock(return_value=client))
    task = asyncio.create_task(transport.connect())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.is_connected is False
    assert transport._client is None


async def test_static_color_clears_effect_only_after_success() -> None:
    state = ElkState(effect=42)
    device = ElkDevice(_transport(FakeClient()), FakeProtocol(), state)
    await device.set_color((20, 30, 40))
    assert state.effect is None


async def test_native_brightness_preserves_running_effect() -> None:
    protocol = FakeProtocol()
    protocol.model = SimpleNamespace(
        get_brightness_cmd=lambda _model, value: [value],
        get_color_cmd=FakeModel.get_color_cmd,
    )
    state = ElkState(effect=42)
    client = FakeClient()
    device = ElkDevice(_transport(client), protocol, state)
    await device.set_brightness(128)
    assert state.effect == 42
    assert client.writes == [bytes([128])]


@pytest.mark.parametrize(
    ("properties", "resolved", "response"),
    [
        (["write"], True, True),
        (["write-without-response"], True, False),
        (["write", "write-without-response"], True, False),
        (["read"], False, False),
    ],
)
async def test_resolved_gatt_properties_control_write_mode(properties, resolved, response) -> None:
    characteristic = SimpleNamespace(uuid="write", properties=properties, handle=13)

    class Services:
        @staticmethod
        def get_characteristic(uuid):
            return characteristic if uuid == "write" else None

        def __iter__(self):
            return iter([SimpleNamespace(uuid="service", characteristics=[characteristic])])

    transport = _transport(FakeClient())
    transport._protocol = SimpleNamespace(
        read_uuid=lambda: "missing-optional-notify",
        write_uuid=lambda: "write",
        refine_by_handle=lambda _handle: None,
    )
    assert transport._resolve_characteristics(Services()) is resolved
    assert transport._write_requires_response is response
