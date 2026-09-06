"""Constants and asynchronously populated effect definitions."""

from enum import Enum

DOMAIN = "elkbledom"
CONF_RESET = "reset"
CONF_DELAY = "delay"
CONF_MODEL = "model"
AUTOMATIC_MODEL = "__automatic__"
CONF_EFFECTS_CLASS = "effects_class"

DEFAULT_RESET = False
DEFAULT_DELAY = 20
MIN_DELAY = 0
MAX_DELAY = 86400

CONF_BRIGHTNESS_MODE = "brightness_mode"
BRIGHTNESS_MODES = ["auto", "rgb", "native"]
DEFAULT_BRIGHTNESS_MODE = "auto"

CONFIG_ENTRY_VERSION = 1
CONFIG_ENTRY_MINOR_VERSION = 2


class MIC_EFFECTS(Enum):
    """Microphone effects shared by the entity platform."""

    mic_energic = 0x80
    mic_rhythm = 0x81
    mic_spectrum = 0x82
    mic_rolling = 0x83
    mic_effect_4 = 0x84
    mic_effect_5 = 0x85
    mic_effect_6 = 0x86
    mic_effect_7 = 0x87


MIC_EFFECTS_list = [member.name for member in MIC_EFFECTS]


class WEEK_DAYS(Enum):
    """Scheduler weekday mask."""

    monday = 0x01
    tuesday = 0x02
    wednesday = 0x04
    thursday = 0x08
    friday = 0x10
    saturday = 0x20
    sunday = 0x40
    all = 0x7F
    week_days = 0x1F
    weekend_days = 0x60
    none = 0x00


# Filled by ensure_models_loaded on Home Assistant's executor. Keeping these
# containers stable is important because platform modules import their objects.
EFFECTS = Enum("EFFECTS", {})
EFFECTS_list: list[str] = []
EFFECTS_MAP: dict[str, type[Enum]] = {}
EFFECTS_LIST_MAP: dict[str, list[str]] = {}


def effects_list_name_for_class(class_name: str) -> str:
    """Return the definitions-list key paired with an effects enum class."""
    if class_name == "EFFECTS":
        return "EFFECTS_list"
    if class_name.startswith("EFFECTS_"):
        return f"EFFECTS_list_{class_name.removeprefix('EFFECTS_')}"
    return f"{class_name}_list"


def populate_effects_from_data(definitions: dict) -> None:
    """Populate effect registries from JSON already read off the event loop."""
    effects_defs = definitions.get("effects_definitions", {}) or {}
    effects_lists = definitions.get("effects_lists", {}) or {}

    effect_enums = {}
    for class_name, values in effects_defs.items():
        effect_enums[class_name] = Enum(class_name, values)

    effect_lists = {}
    for list_name, list_values in effects_lists.items():
        effect_lists[list_name] = list(list_values)

    for class_name, effect_enum in effect_enums.items():
        list_name = effects_list_name_for_class(class_name)
        if list_name not in effect_lists or not set(effect_lists[list_name]).issubset(
            effect_enum.__members__
        ):
            raise ValueError(f"Effect definitions and list do not match: {class_name}")
    if not effect_enums.get("EFFECTS") or not effect_lists.get("EFFECTS_list"):
        raise ValueError("Default effect definitions are missing")

    EFFECTS_MAP.clear()
    EFFECTS_MAP.update(effect_enums)
    EFFECTS_LIST_MAP.clear()
    EFFECTS_LIST_MAP.update(effect_lists)

    # Mutate the imported fallback list instead of replacing it.
    EFFECTS_list.clear()
    EFFECTS_list.extend(EFFECTS_LIST_MAP.get("EFFECTS_list", []))
