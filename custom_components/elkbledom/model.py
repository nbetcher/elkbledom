"""Model configuration manager for LED strips"""

import json
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import EFFECTS_LIST_MAP, EFFECTS_MAP, populate_effects_from_data
from .limits import EffectSpeedLimits

LOGGER = logging.getLogger(__name__)

# Models data key - must match __init__.py
MODELS_DATA_KEY = "elkbledom_models"


async def ensure_models_loaded(hass: HomeAssistant) -> dict[str, dict]:
    """Ensure models are loaded in hass.data, loading them if necessary."""
    if MODELS_DATA_KEY in hass.data:
        return hass.data[MODELS_DATA_KEY]

    # Need to load models
    models_file = Path(__file__).parent / "models.json"

    def _load_json():
        try:
            if not models_file.exists():
                LOGGER.error("models.json file not found at: %s", models_file)
                return {}

            content = models_file.read_text(encoding="utf-8")
            models_array = json.loads(content)

            # Convert array to dictionary for internal use
            # Each model gets a unique internal key: name or name_handle
            models_dict = {}
            for model in models_array:
                model_name = model.get("name", "Unknown")
                handle = model.get("handle")

                # Create unique internal key
                internal_key = f"{model_name}#{handle}" if handle is not None else model_name

                # Store the model data with the internal key
                models_dict[internal_key] = model.copy()
                # Ensure 'name' field is preserved
                models_dict[internal_key]["name"] = model_name

            LOGGER.debug("Loaded %d models from models.json array", len(models_dict))
            return models_dict
        except json.JSONDecodeError as e:
            LOGGER.error("Error decoding models.json: %s", e)
            return {}
        except (OSError, UnicodeError) as err:
            LOGGER.error("Error loading models.json from %s: %s", models_file, err)
            return {}

    models = await hass.async_add_executor_job(_load_json)

    definitions_file = Path(__file__).parent / "definitions.json"

    def _load_definitions_json():
        try:
            return json.loads(definitions_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as err:
            LOGGER.error("Error loading definitions.json from %s: %s", definitions_file, err)
            return {}

    populate_effects_from_data(await hass.async_add_executor_job(_load_definitions_json))
    if not models:
        raise ValueError("No model definitions could be loaded")
    # Publish only after both tables are ready. Concurrent flows must never see
    # a model cache that advertises readiness while effects are still loading.
    hass.data[MODELS_DATA_KEY] = models
    return hass.data[MODELS_DATA_KEY]


def get_models_data(hass: HomeAssistant) -> dict[str, dict]:
    """Get models data from hass.data (loaded asynchronously in __init__.py)."""
    return hass.data.get(MODELS_DATA_KEY, {})


class Model:
    """Model configuration manager for LED strips"""

    def __init__(self, hass: HomeAssistant | None = None):
        """Initialize Model with optional hass instance for data access."""
        self._hass = hass
        self._models: dict[str, dict] = {}
        if hass is not None:
            self._models = get_models_data(hass)

    def get_models(self) -> list[str]:
        """Get list of all supported model names (without internal keys)"""
        # Return unique model names
        model_names = set()
        for internal_key, model_data in self._models.items():
            model_names.add(model_data.get("name", internal_key.split("#")[0]))
        return list(model_names)

    def get_models_display_dict(self) -> dict[str, str]:
        """Get dictionary of internal keys to display names.

        For models with handles, display as "Model Name (handle X)".
        This is used in config flow UI to show differentiated model names.

        Returns:
            Dict mapping internal key -> display name (for vol.In)
        """
        display_dict = {}

        for internal_key, model_data in self._models.items():
            model_name = model_data.get("name", internal_key.split("#")[0])
            handle = model_data.get("handle")

            # Create display name
            display_name = f"{model_name} (handle {handle})" if handle is not None else model_name

            # Map internal_key -> display_name for vol.In
            display_dict[internal_key] = display_name

        return display_dict

    def get_display_name_for_model(self, internal_key: str) -> str | None:
        """Get display name for a specific model by internal key.

        Args:
            internal_key: The internal key (e.g., "ELK-BLEDOM#13" or "ELK-BLEDOM")

        Returns:
            Display name (may include handle info)
        """
        if internal_key not in self._models:
            return internal_key

        model_data = self._models[internal_key]
        model_name = model_data.get("name", internal_key.split("#")[0])
        handle = model_data.get("handle")

        if handle is not None:
            return f"{model_name} (handle {handle})"
        return model_name

    def get_model_name_from_display(self, value: str) -> str:
        """Convert form value to internal key.

        Since we now use {internal_key: display_name} in vol.In,
        the form already returns the internal_key directly.

        Args:
            value: Value from form (already internal_key)

        Returns:
            Internal key
        """
        # Value is already the internal_key from the form
        return value

    def detect_model(self, device_name: str) -> str | None:
        """Detect model from device name.

        Returns:
            Internal key of the detected model
        """
        device_name_lower = device_name.lower()

        # Collect all matching models by device name
        matching_models = []
        for internal_key, model_data in self._models.items():
            model_name = model_data.get("name", internal_key.split("#")[0])
            if device_name_lower.startswith(model_name.lower()):
                matching_models.append((internal_key, model_data))

        if not matching_models:
            return None

        # Sort by model name length (longest first) for most specific match
        matching_models.sort(key=lambda x: len(x[1].get("name", "")), reverse=True)

        # If only one match, return it
        if len(matching_models) == 1:
            return matching_models[0][0]

        # Multiple matches: prefer the one without handle (generic version)
        for internal_key, model_data in matching_models:
            if model_data.get("handle") is None:
                LOGGER.debug("Model detected by name (generic): %s", internal_key)
                return internal_key

        # All have handles, return first (most specific)
        return matching_models[0][0]

    def detect_model_by_handle(self, device_name: str, char_handle: int) -> str | None:
        """Detect model from device name and characteristic handle.

        When multiple models have the same name but different handles,
        the handle is used to select the correct one.

        Returns:
            Internal key of the detected model
        """
        device_name_lower = device_name.lower()

        # Collect all matching models by name
        matching_models = []
        for internal_key, model_data in self._models.items():
            model_name = model_data.get("name", internal_key.split("#")[0])
            if device_name_lower.startswith(model_name.lower()):
                matching_models.append((internal_key, model_data))

        if not matching_models:
            return None

        # If only one match, return it
        if len(matching_models) == 1:
            return matching_models[0][0]

        # Multiple matches: prefer the one with matching handle
        for internal_key, model_data in matching_models:
            model_handle = model_data.get("handle")
            if model_handle is not None and char_handle == model_handle:
                LOGGER.debug(
                    "Model detected by handle: %s (handle: 0x%04x)", internal_key, char_handle
                )
                return internal_key

        # No handle match: return the one without handle (generic version)
        for internal_key, model_data in matching_models:
            if model_data.get("handle") is None:
                LOGGER.debug("Model detected by name (no handle match): %s", internal_key)
                return internal_key

        # Fallback: return first match
        return matching_models[0][0]

    def get_handle(self, internal_key: str) -> int | None:
        """Get handle for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("handle")
        return None

    def get_write_uuid(self, internal_key: str) -> str | None:
        """Get write characteristic UUID for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("write_uuid")
        return None

    def get_read_uuid(self, internal_key: str) -> str | None:
        """Get read characteristic UUID for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("read_uuid")
        return None

    def get_supports_mic(self, internal_key: str) -> bool:
        """Whether the model is known to support the external microphone.

        Models opt in via an optional "mic": true flag in models.json. When
        absent (the default), the mic entities are still created but disabled in
        the entity registry so they don't clutter unsupported devices.
        """
        if internal_key in self._models:
            model = self._models[internal_key]
            capabilities = model.get("capabilities", {})
            return bool(model.get("mic", False) or capabilities.get("microphone", False))
        return False

    def get_turn_on_cmd(self, internal_key: str) -> list[int] | None:
        """Get turn on command for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("commands", {}).get("turn_on")
        return None

    def get_turn_off_cmd(self, internal_key: str) -> list[int] | None:
        """Get turn off command for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("commands", {}).get("turn_off")
        return None

    @staticmethod
    def _scale_intensity(intensity: int) -> int:
        """Convert a 0-255 intensity to the device's 0-100 'i' value.

        A non-zero input is floored to 1 so the lowest brightness still dims the
        strip instead of mapping to 0 (off); an input of 0 stays 0 so turning the
        white channel fully off is preserved. Shared by get_white_cmd and
        get_brightness_cmd so the conversion can't drift between the two.
        """
        intensity = int(intensity)
        value = int(intensity * 100 / 255)
        if value == 0 and intensity > 0:
            value = 1
        return value

    def get_white_cmd(self, internal_key: str, intensity: int) -> list[int] | None:
        """Get white command for model with intensity by internal key"""
        if internal_key in self._models:
            cmd = self._models[internal_key].get("commands", {}).get("white", []).copy()
            if "i" not in cmd:
                return None
            # Replace 'i' placeholder with the scaled intensity value
            scaled = self._scale_intensity(intensity)
            return [scaled if x == "i" else x for x in cmd]
        return None

    def get_effect_speed_limits(self, internal_key: str) -> EffectSpeedLimits:
        """Read model-specific limits without assuming a full-byte device range."""
        limits = self._models.get(internal_key, {}).get("effect_speed_range", {})
        if not isinstance(limits, dict):
            raise ValueError(f"Invalid effect_speed_range for {internal_key}")
        return EffectSpeedLimits(limits.get("min", 0), limits.get("max", 100))

    def supports_effect_speed(self, internal_key: str) -> bool:
        """A fixed captured frame is not evidence of an adjustable speed control."""
        command = self._models.get(internal_key, {}).get("commands", {}).get("effect_speed", [])
        return "v" in command

    def get_effect_speed_cmd(self, internal_key: str, value: int | float) -> list[int] | None:
        """Get effect speed command for model by internal key"""
        if self.supports_effect_speed(internal_key):
            value = self.get_effect_speed_limits(internal_key).validate(value)
            cmd = self._models[internal_key].get("commands", {}).get("effect_speed", []).copy()
            # Replace 'v' placeholder with value
            return [int(value) if x == "v" else x for x in cmd]
        return None

    def get_effect_cmd(self, internal_key: str, value: int) -> list[int] | None:
        """Get effect command for model by internal key"""
        if internal_key in self._models:
            cmd = self._models[internal_key].get("commands", {}).get("effect", []).copy()
            if "v" not in cmd or isinstance(value, bool) or not isinstance(value, int):
                return None
            if not 0 <= value <= 255:
                return None
            # Replace 'v' placeholder with value
            result = [value if x == "v" else x for x in cmd]
            if not all(type(byte) is int and 0 <= byte <= 255 for byte in result):
                return None
            return result
        return None

    def get_color_temp_cmd(self, internal_key: str, warm: int, cold: int) -> list[int] | None:
        """Get color temperature command for model by internal key"""
        if internal_key in self._models:
            cmd = self._models[internal_key].get("commands", {}).get("color_temp", []).copy()
            if not {"w", "c"}.issubset(cmd):
                return None
            # Replace 'w' and 'c' placeholders with warm and cold values
            result = []
            for x in cmd:
                if x == "w":
                    result.append(int(warm))
                elif x == "c":
                    result.append(int(cold))
                else:
                    result.append(x)
            return result
        return None

    def get_color_cmd(self, internal_key: str, r: int, g: int, b: int) -> list[int] | None:
        """Get color command for model by internal key"""
        if internal_key in self._models:
            cmd = self._models[internal_key].get("commands", {}).get("color", []).copy()
            if not {"r", "g", "b"}.issubset(cmd):
                return None
            # Replace 'r', 'g', 'b' placeholders with RGB values
            result = []
            for x in cmd:
                if x == "r":
                    result.append(r)
                elif x == "g":
                    result.append(g)
                elif x == "b":
                    result.append(b)
                else:
                    result.append(x)
            return result
        return None

    def get_brightness_cmd(self, internal_key: str, intensity: int) -> list[int] | None:
        """Get brightness command for model by internal key"""
        if internal_key in self._models:
            cmd = self._models[internal_key].get("commands", {}).get("brightness", []).copy()
            if "i" not in cmd:
                return None
            # Replace 'i' placeholder with the scaled intensity value
            scaled = self._scale_intensity(intensity)
            return [scaled if x == "i" else x for x in cmd]
        return None

    def get_query_cmd(self, internal_key: str) -> list[int] | None:
        """Get query command for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("commands", {}).get("query")
        return None

    def get_sync_time_cmd(
        self, internal_key: str, hour: int, minute: int, second: int, day_of_week: int
    ) -> list[int]:
        """Get sync time command (same for all models).

        Frame: 7E 07 83 <hour> <min> <sec> <weekday Sun=0..Sat=6> FF EF.
        byte[1]=0x07 and byte[7]=0xFF per the vendor app; an earlier 0x00/0x00
        form was never accepted by the firmware.
        """
        return [0x7E, 0x07, 0x83, hour, minute, second, day_of_week, 0xFF, 0xEF]

    def get_custom_time_cmd(
        self, internal_key: str, hour: int, minute: int, second: int, day_of_week: int
    ) -> list[int]:
        """Get custom time command (same frame as get_sync_time_cmd)."""
        return [0x7E, 0x07, 0x83, hour, minute, second, day_of_week, 0xFF, 0xEF]

    @staticmethod
    def _sanitize_kelvin(value, default: int) -> int:
        """Coerce JSON-loaded Kelvin values into Home Assistant's range."""
        try:
            value = int(value)
        except TypeError, ValueError:
            return default
        return value if 1000 <= value <= 40000 else default

    def get_min_color_temp_kelvin(self, internal_key: str) -> int:
        """Get minimum color temperature in Kelvin for model by internal key"""
        if internal_key in self._models:
            raw = self._models[internal_key].get("color_temp_range", {}).get("min_kelvin", 1800)
            return self._sanitize_kelvin(raw, 1800)
        return 1800

    def get_max_color_temp_kelvin(self, internal_key: str) -> int:
        """Get maximum color temperature in Kelvin for model by internal key"""
        if internal_key in self._models:
            raw = self._models[internal_key].get("color_temp_range", {}).get("max_kelvin", 7000)
            value = self._sanitize_kelvin(raw, 7000)
            minimum = self.get_min_color_temp_kelvin(internal_key)
            return value if value > minimum else max(minimum + 100, 7000)
        return 7000

    def get_effects_class(self, internal_key: str) -> str:
        """Get effects class name for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("effects_class", "EFFECTS")
        return "EFFECTS"

    def get_effects_list(self, internal_key: str) -> str:
        """Get effects list name for model by internal key"""
        if internal_key in self._models:
            return self._models[internal_key].get("effects_list", "EFFECTS_list")
        return "EFFECTS_list"

    def get_effect_value(self, effects_class_name: str, effect_name: str) -> int | None:
        """Get a value from the asynchronously loaded effects definitions."""
        effects_class = EFFECTS_MAP.get(effects_class_name)
        if effects_class is None or effect_name not in effects_class.__members__:
            return None
        return effects_class[effect_name].value

    def get_effects_enum(self, effects_class_name: str) -> type | None:
        """Return an asynchronously loaded effect enum."""
        return EFFECTS_MAP.get(effects_class_name)

    def get_effects_list_values(self, effects_list_name: str) -> list[str]:
        """Return one asynchronously loaded effect-name list."""
        return EFFECTS_LIST_MAP.get(effects_list_name, [])

    def get_all_effects_definitions(self) -> dict[str, dict[str, int]]:
        """Return serializable copies of all effect definitions."""
        return {
            name: {member_name: member.value for member_name, member in enum.__members__.items()}
            for name, enum in EFFECTS_MAP.items()
        }

    def get_all_effects_lists(self) -> dict[str, list[str]]:
        """Return copies of all effect-name lists."""
        return {name: list(values) for name, values in EFFECTS_LIST_MAP.items()}
