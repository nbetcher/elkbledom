"""Pure value limits shared by model encoding, commands, and state restoration."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class EffectSpeedLimits:
    """Conservative defaults; only evidenced model definitions may widen them."""

    minimum: int = 0
    maximum: int = 100

    def __post_init__(self) -> None:
        if (
            type(self.minimum) is not int
            or type(self.maximum) is not int
            or not 0 <= self.minimum <= self.maximum <= 255
        ):
            raise ValueError("Effect speed limits must be ordered integer bytes")

    def validate(self, value: int | float) -> int:
        """Reject invalid commands instead of silently changing the request."""
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or int(value) != value
            or not self.minimum <= value <= self.maximum
        ):
            raise ValueError(f"Effect speed must be an integer in {self.minimum}-{self.maximum}")
        return int(value)

    def clamp(self, value: int | float | str) -> int:
        """Tolerate legacy numeric recorder values, clamped to today's bounds."""
        if isinstance(value, bool):
            raise ValueError("Boolean is not an effect speed")
        parsed = float(value)
        if not isfinite(parsed):
            raise ValueError("Effect speed must be finite")
        return max(self.minimum, min(int(parsed), self.maximum))


DEFAULT_EFFECT_SPEED_LIMITS = EffectSpeedLimits()
