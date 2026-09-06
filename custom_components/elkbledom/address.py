"""Bluetooth address normalization helpers."""

from __future__ import annotations

import re

_HEX_MAC = re.compile(r"^[0-9a-fA-F]{12}$")
_COLON_OR_DASH_MAC = re.compile(r"^[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}$")
_DOTTED_MAC = re.compile(r"^[0-9a-fA-F]{4}(?:\.[0-9a-fA-F]{4}){2}$")

ENTITY_UNIQUE_ID_SUFFIXES = (
    "",
    "_effect_speed",
    "_mic_sensitivity",
    "_mic_effect",
    "_brightness_mode",
    "_mic_enable",
)


def normalize_address(address: str) -> str:
    """Return a stable lowercase MAC or CoreBluetooth UUID address."""
    value = str(address).strip()
    if _COLON_OR_DASH_MAC.fullmatch(value):
        compact = value.replace(":", "").replace("-", "")
    elif _DOTTED_MAC.fullmatch(value):
        compact = value.replace(".", "")
    elif _HEX_MAC.fullmatch(value):
        compact = value
    else:
        return value.lower()
    compact = compact.lower()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def addresses_equal(left: str, right: str) -> bool:
    """Compare Bluetooth addresses after canonicalization."""
    return normalize_address(left) == normalize_address(right)


def normalize_entity_unique_id(unique_id: str, old_address: str, canonical: str) -> str:
    """Canonicalize an address prefix in one of this integration's unique IDs."""
    if unique_id.casefold().startswith(old_address.casefold()):
        suffix = unique_id[len(old_address) :]
        if suffix in ENTITY_UNIQUE_ID_SUFFIXES:
            return f"{canonical}{suffix}"

    for suffix in ENTITY_UNIQUE_ID_SUFFIXES:
        if suffix and not unique_id.endswith(suffix):
            continue
        address_part = unique_id[: -len(suffix)] if suffix else unique_id
        if addresses_equal(address_part, canonical):
            return f"{canonical}{suffix}"
    return unique_id
