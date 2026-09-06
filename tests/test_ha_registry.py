"""Exercise migration against Home Assistant's real immutable registry objects."""

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.elkbledom import async_migrate_entry


async def test_real_registry_migration_preserves_entities_and_linked_helpers(tmp_path) -> None:
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    entry = ConfigEntry(
        data={"mac": "AA:BB:CC:DD:EE:FF", "name": "Kitchen", "model": "ELK-BLEDOM"},
        discovery_keys=MappingProxyType({}),
        domain="elkbledom",
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title="Kitchen",
        unique_id="AA:BB:CC:DD:EE:FF",
        version=1,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    old_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("elkbledom", "AA:BB:CC:DD:EE:FF")},
        name="Kitchen",
    )
    duplicate_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("elkbledom", "aa:bb:cc:dd:ee:ff")},
        name="Duplicate",
    )
    original = entity_registry.async_get_or_create(
        "light",
        "elkbledom",
        "AA:BB:CC:DD:EE:FF",
        config_entry=entry,
        device_id=old_device.id,
        suggested_object_id="established",
    )
    duplicate = entity_registry.async_get_or_create(
        "light",
        "elkbledom",
        "aa:bb:cc:dd:ee:ff",
        config_entry=entry,
        device_id=duplicate_device.id,
        suggested_object_id="duplicate",
    )
    entity_registry.async_update_entity(duplicate.entity_id, aliases={"Reading strip"})
    entity_registry.async_update_entity_options(
        original.entity_id, "light", {"default_transition": 0, "explicit_none": None}
    )
    entity_registry.async_update_entity_options(
        duplicate.entity_id,
        "light",
        {"default_transition": 5, "explicit_none": 10, "custom_option": False},
    )
    entity_registry.async_update_entity_options(
        duplicate.entity_id, "conversation", {"should_expose": False}
    )
    helper = entity_registry.async_get_or_create(
        "sensor",
        "template",
        "linked_helper",
        device_id=duplicate_device.id,
        suggested_object_id="linked_helper",
    )
    try:
        assert await async_migrate_entry(hass, entry) is True
        assert entry.version == 1
        assert entry.minor_version == 2
        assert entry.data["model"] == "ELK-BLEDOM"
        assert entry.unique_id == "aa:bb:cc:dd:ee:ff"
        assert entity_registry.async_get(duplicate.entity_id) is None
        survivor = entity_registry.async_get(original.entity_id)
        assert survivor.unique_id == "aa:bb:cc:dd:ee:ff"
        assert "Reading strip" in survivor.aliases
        expected_options = {
            "light": {"default_transition": 0, "explicit_none": None, "custom_option": False},
            "conversation": {"should_expose": False},
        }
        assert dict(survivor.options) == expected_options
        assert entity_registry.async_get(helper.entity_id).device_id == old_device.id
        assert device_registry.async_get(duplicate_device.id) is None
        assert await async_migrate_entry(hass, entry) is True
        assert dict(entity_registry.async_get(original.entity_id).options) == expected_options
    finally:
        await hass.async_stop(force=True)
