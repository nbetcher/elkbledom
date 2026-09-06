"""Optimistic state cache for the elkbledom integration.

Holds the device's optimistically-tracked state (the values entities report to
Home Assistant between/without readbacks -- these strips do not emit reliable
status frames, so command methods are the source of truth). Extracted verbatim
from the ``BLEDOMInstance`` __init__ cache (elkbledom.py:168-180); defaults MUST
match that source exactly. No BLE, no HA imports -- typing only.
"""

from contextlib import suppress
from dataclasses import dataclass

from .limits import DEFAULT_EFFECT_SPEED_LIMITS, EffectSpeedLimits

# Sentinel distinguishing "argument not provided" from an explicit None, so
# restore() can skip untouched fields without treating None as a real value.
_MISSING = object()


@dataclass
class ElkState:
    # Optimistic cache. Defaults MUST match elkbledom.py:168-180 exactly.
    is_on: bool | None = None  # 168
    rgb_color: tuple[int, int, int] | None = None  # 169
    rgb_color_base: tuple[int, int, int] = (255, 255, 255)  # 170  base w/o brightness scaling
    brightness: int = 255  # 171
    effect: int | None = None  # 172
    effect_speed: int = 50  # Conservative default, constrained by the selected model.
    color_temp_kelvin: int | None = None  # 174
    mic_effect: int | None = None  # 175
    mic_sensitivity: int = 50  # 176
    mic_enabled: bool = False  # 177
    color_temp: int | None = None  # 180  0-100 warm; set_color_temp path

    def get_color_base(self) -> tuple[int, int, int]:
        """The unscaled base RGB used for brightness scaling (elkbledom.py:264).

        ElkDevice writes rgb_color / rgb_color_base directly (the couplings differ
        per path -- e.g. the color-temp RGB fallback deliberately decouples base),
        so there are no apply_color helpers; only this read accessor is shared.
        """
        return self.rgb_color_base

    # --- optional convenience for the entity-migration phase (Phase 3) ---
    def restore(
        self,
        *,
        is_on=_MISSING,
        brightness=_MISSING,
        rgb_color=_MISSING,
        color_temp_kelvin=_MISSING,
        effect=_MISSING,
        effect_speed=_MISSING,
        effect_speed_limits: EffectSpeedLimits = DEFAULT_EFFECT_SPEED_LIMITS,
        mic_enabled=_MISSING,
        couple_base: bool = True,
    ) -> None:
        """Assign only provided fields. When rgb_color is provided and couple_base,
        also set rgb_color_base = rgb_color (mirrors light.py:186). None-guards and
        the try/except around RGB stay in the caller; restore() only skips _MISSING.
        """
        if is_on is not _MISSING and is_on is not None:
            self.is_on = bool(is_on)
        if brightness is not _MISSING and brightness is not None:
            with suppress(TypeError, ValueError, OverflowError):
                self.brightness = max(1, min(int(brightness), 255))
        if rgb_color is not _MISSING:
            with suppress(TypeError, ValueError, OverflowError):
                rgb = tuple(max(0, min(int(channel), 255)) for channel in rgb_color)
                if len(rgb) == 3:
                    self.rgb_color = rgb
                    if couple_base:
                        self.rgb_color_base = rgb
        if color_temp_kelvin is not _MISSING and color_temp_kelvin is not None:
            with suppress(TypeError, ValueError, OverflowError):
                self.color_temp_kelvin = max(1000, min(int(color_temp_kelvin), 40000))
        if effect is not _MISSING:
            self.effect = effect
        if effect_speed is not _MISSING and effect_speed is not None:
            with suppress(TypeError, ValueError, OverflowError):
                self.effect_speed = effect_speed_limits.clamp(effect_speed)
        if mic_enabled is not _MISSING:
            self.mic_enabled = mic_enabled
