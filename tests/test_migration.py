"""Config-entry migration regression tests."""

from __future__ import annotations

from types import SimpleNamespace

import custom_components.elkbledom as integration


def _entity(entity_id: str, unique_id: str, created_at: str, device_id: str, labels: set[str]):
    return SimpleNamespace(
        aliases=[],
        area_id=None,
        categories={},
        config_entry_id="entry",
        created_at=created_at,
        device_id=device_id,
        disabled_by=None,
        domain="light",
        entity_category=None,
        entity_id=entity_id,
        hidden_by=None,
        icon=None,
        labels=labels,
        name=None,
        platform="elkbledom",
        unique_id=unique_id,
    )


def _device(device_id: str, address: str, created_at: str, labels: set[str]):
    return SimpleNamespace(
        area_id=None,
        config_entry_id="entry",
        connections={("bluetooth", address)},
        created_at=created_at,
        disabled_by=None,
        id=device_id,
        identifiers={("elkbledom", address)},
        labels=labels,
        name_by_user=None,
    )


class FakeEntityRegistry:
    def __init__(self, entities) -> None:
        self.entities = {entity.entity_id: entity for entity in entities}

    def async_get_entity_id(self, domain, platform, unique_id):
        for entity in self.entities.values():
            if (
                entity.domain == domain
                and entity.platform == platform
                and entity.unique_id == unique_id
            ):
                return entity.entity_id
        return None

    def async_update_entity(self, entity_id, **changes):
        entity = self.entities[entity_id]
        if "new_unique_id" in changes:
            entity.unique_id = changes.pop("new_unique_id")
        for key, value in changes.items():
            setattr(entity, key, value)
        return entity

    def async_remove(self, entity_id):
        self.entities.pop(entity_id)


class FakeDeviceRegistry:
    def __init__(self, devices) -> None:
        self.devices = {device.id: device for device in devices}

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        return next(
            (
                device
                for device in self.devices.values()
                if device.config_entry_id == config_entry_id and identifier in device.identifiers
            ),
            None,
        )

    def async_get_device_by_connection(self, connection, config_entry_id):
        return next(
            (
                device
                for device in self.devices.values()
                if device.config_entry_id == config_entry_id and connection in device.connections
            ),
            None,
        )

    def async_update_device(self, device_id, **changes):
        device = self.devices[device_id]
        if "new_identifiers" in changes:
            device.identifiers = changes.pop("new_identifiers")
        if "new_connections" in changes:
            device.connections = changes.pop("new_connections")
        for key, value in changes.items():
            setattr(device, key, value)
        return device

    def async_remove_device(self, device_id):
        self.devices.pop(device_id)


class FakeConfigEntries:
    def __init__(self) -> None:
        self.update: dict | None = None

    @staticmethod
    def async_entries(_domain):
        return []

    def async_update_entry(self, entry, **changes):
        self.update = changes
        for key, value in changes.items():
            setattr(entry, key, value)


async def test_migration_preserves_oldest_rows_and_merges_metadata(monkeypatch) -> None:
    old_address = "AA:BB:CC:DD:EE:FF"
    canonical = "aa:bb:cc:dd:ee:ff"
    old_entity = _entity(
        "light.established", old_address, "2025-01-01", "old-device", {"established"}
    )
    duplicate_entity = _entity("light.duplicate", canonical, "2026-01-01", "new-device", {"new"})
    old_device = _device("old-device", old_address, "2025-01-01", {"established"})
    duplicate_device = _device("new-device", canonical, "2026-01-01", {"new"})
    entity_registry = FakeEntityRegistry([old_entity, duplicate_entity])
    device_registry = FakeDeviceRegistry([old_device, duplicate_device])
    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    entry = SimpleNamespace(
        data={"mac": old_address, "name": "Kitchen"},
        entry_id="entry",
        minor_version=1,
        options={},
        unique_id=old_address,
        version=1,
    )

    monkeypatch.setattr(integration.er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(
        integration.er,
        "async_entries_for_config_entry",
        lambda registry, _entry_id: list(registry.entities.values()),
    )
    monkeypatch.setattr(
        integration.er,
        "async_entries_for_device",
        lambda registry, device_id, **_kwargs: [
            row for row in registry.entities.values() if row.device_id == device_id
        ],
    )
    monkeypatch.setattr(integration.dr, "async_get", lambda _hass: device_registry)
    monkeypatch.setattr(
        integration.dr,
        "async_entries_for_config_entry",
        lambda registry, _entry_id: list(registry.devices.values()),
    )

    assert await integration.async_migrate_entry(hass, entry) is True
    assert set(entity_registry.entities) == {"light.established"}
    assert old_entity.unique_id == canonical
    assert old_entity.labels == {"established", "new"}
    assert old_entity.device_id == "old-device"
    assert set(device_registry.devices) == {"old-device"}
    assert old_device.labels == {"established", "new"}
    assert old_device.identifiers == {("elkbledom", canonical)}
    assert old_device.connections == {("bluetooth", canonical)}
    assert config_entries.update == {
        "data": {"mac": canonical, "name": "Kitchen"},
        "options": {},
        "unique_id": canonical,
        "version": 1,
        "minor_version": 2,
    }


async def test_empty_address_migration_still_advances_minor_version() -> None:
    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    entry = SimpleNamespace(data={}, minor_version=1, options={}, version=1)

    assert await integration.async_migrate_entry(hass, entry) is True
    assert config_entries.update == {"version": 1, "minor_version": 2}
