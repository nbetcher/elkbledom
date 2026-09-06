"""Regression tests for the documented BLE discovery utility."""

from types import SimpleNamespace

import pytest

from BTScan import _write_response_required


@pytest.mark.parametrize(
    ("properties", "expected_response"),
    [
        (["write"], True),
        (["write-without-response"], False),
        (["write", "write-without-response"], False),
    ],
)
def test_btscan_honors_characteristic_write_properties(
    properties: list[str], expected_response: bool
) -> None:
    characteristic = SimpleNamespace(properties=properties)
    services = SimpleNamespace(get_characteristic=lambda _uuid: characteristic)
    client = SimpleNamespace(services=services)

    assert _write_response_required(client, "write-uuid") is expected_response


@pytest.mark.parametrize(
    ("characteristic", "message"),
    [
        (None, "unavailable"),
        (SimpleNamespace(properties=["read"]), "not writable"),
    ],
)
def test_btscan_rejects_invalid_write_characteristics(characteristic, message: str) -> None:
    services = SimpleNamespace(get_characteristic=lambda _uuid: characteristic)
    client = SimpleNamespace(services=services)

    with pytest.raises(ValueError, match=message):
        _write_response_required(client, "write-uuid")
