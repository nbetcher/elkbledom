"""Optimistic state cache for the elkbledom integration.

Holds the device's optimistically-tracked state (the values entities report to
Home Assistant between/without readbacks -- these strips do not emit reliable
status frames, so command methods are the source of truth). Extracted verbatim
from the ``BLEDOMInstance`` __init__ cache (elkbledom.py:168-180); defaults MUST
match that source exactly. No BLE, no HA imports -- typing only.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

# Sentinel distinguishing "argument not provided" from an explicit None, so
# restore() can skip untouched fields without treating None as a real value.
_MISSING = object()


@dataclass
class ElkState:
    # Optimistic cache. Defaults MUST match elkbledom.py:168-180 exactly.
    is_on: Optional[bool] = None                            # 168
    rgb_color: Optional[Tuple[int, int, int]] = None        # 169
    rgb_color_base: Tuple[int, int, int] = (255, 255, 255)  # 170  base w/o brightness scaling
    brightness: int = 255                                   # 171
    effect: Optional[int] = None                            # 172
    effect_speed: int = 50                                  # 173  (NOT 0 -- device range 0-100, default medium)
    color_temp_kelvin: Optional[int] = None                 # 174
    mic_effect: Optional[int] = None                        # 175
    mic_sensitivity: int = 50                               # 176
    mic_enabled: bool = False                               # 177
    color_temp: Optional[int] = None                        # 180  0-100 warm; set_color_temp path

    # --- base/rgb coupling helpers (the exact couplings that exist today) ---
    def apply_color(self, rgb: Tuple[int, int, int], is_base: bool) -> None:
        """Mirror set_color 555-558: always set rgb_color; set base only if is_base."""
        self.rgb_color = rgb
        if is_base:
            self.rgb_color_base = rgb

    def apply_scaled_color(self, scaled_rgb: Tuple[int, int, int]) -> None:
        """Mirror set_brightness RGB path 593: rgb_color = scaled, base preserved."""
        self.rgb_color = scaled_rgb

    def get_color_base(self) -> Tuple[int, int, int]:
        """Mirror elkbledom.py:264."""
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
        mic_enabled=_MISSING,
        couple_base: bool = True,
    ) -> None:
        """Assign only provided fields. When rgb_color is provided and couple_base,
        also set rgb_color_base = rgb_color (mirrors light.py:186). None-guards and
        the try/except around RGB stay in the caller; restore() only skips _MISSING.
        """
        if is_on is not _MISSING:
            self.is_on = is_on
        if brightness is not _MISSING:
            self.brightness = brightness
        if rgb_color is not _MISSING:
            self.rgb_color = rgb_color
            if couple_base:
                self.rgb_color_base = rgb_color
        if color_temp_kelvin is not _MISSING:
            self.color_temp_kelvin = color_temp_kelvin
        if effect is not _MISSING:
            self.effect = effect
        if effect_speed is not _MISSING:
            self.effect_speed = effect_speed
        if mic_enabled is not _MISSING:
            self.mic_enabled = mic_enabled
