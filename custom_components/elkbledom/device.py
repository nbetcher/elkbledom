"""Intent layer for the elkbledom integration.

``ElkDevice`` turns high-level intents (turn on/off, set colour/brightness/effect,
mic control, schedulers, time sync, update) into protocol frames and delegates
every write to ``BLETransport``. It holds the optimistic ``ElkState`` cache, the
HA brightness-mode policy (auto/rgb/native), and the mic-exit policy. Model
indirection + the model-independent frame builders live in ``ElkProtocol``;
per-model frames come from ``models.json`` via ``Model`` (reached through
``self.protocol.model`` / ``self.protocol.model_name``).

Behaviour is extracted verbatim from the ``BLEDOMInstance`` god object
(elkbledom.py intents 254-820); only ``self.X`` bindings change per the rebind
partition in docs/god-object-refactor.md §3.4. The @retry decorator is *applied*
here (defined in transport.py) so a whole intent — including _exit_mic_mode,
multi-frame writes, and the brightness auto-fallback — is retried as one unit.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .protocol import ElkProtocol
from .state import ElkState
from .transport import MIC_EXIT_SETTLE, UnsupportedCommandError, retry_bluetooth_connection_error

if TYPE_CHECKING:
    from .transport import BLETransport

LOGGER = logging.getLogger(__name__)


class ElkDevice:
    """Intents -> frames -> transport writes; owns state + brightness/mic policy."""

    def __init__(
        self,
        transport: BLETransport,
        protocol: ElkProtocol,
        state: ElkState,
        brightness_mode: str = "auto",
    ) -> None:
        self.transport = transport
        self.protocol = protocol
        self.state = state
        # Brightness mode configuration (auto, rgb, native), seeded from config.
        # Device-local policy knob — NOT part of the optimistic state cache.
        mode = (brightness_mode or "auto").lower()
        self._brightness_mode = mode if mode in ("auto", "rgb", "native") else "auto"

    # ---- decorator-binding proxies (so the copied @retry body is byte-identical) ----
    @property
    def name(self):
        # The @retry decorator logs use self.name.
        return self.transport.name

    def _set_available(self, ok: bool) -> None:
        # The @retry decorator flips availability on final failure via this.
        self.transport.set_available(ok)

    # ---- read-through state used by facade props ----
    @property
    def brightness_mode(self):
        return self._brightness_mode

    @property
    def min_color_temp_kelvin(self):
        return self.protocol.model.get_min_color_temp_kelvin(self.protocol.model_name)

    @property
    def max_color_temp_kelvin(self):
        return self.protocol.model.get_max_color_temp_kelvin(self.protocol.model_name)

    # ---- policy (no retry) ----
    async def apply_brightness_mode(self, mode: str) -> None:
        """Apply new brightness mode and reconnect if needed."""
        mode = (mode or "auto").lower()
        if mode not in ("auto", "rgb", "native"):
            mode = "auto"
        if mode == self._brightness_mode:
            return
        self._brightness_mode = mode
        LOGGER.info("%s: Brightness mode changed to: %s", self.name, mode)

    def get_color_base(self):
        return self.state.get_color_base()

    # ---- mic-exit policy (invariant #7) ----
    async def _exit_mic_mode(self) -> None:
        """Leave music/mic mode before issuing a normal display command.

        The strip stays in music mode -- ignoring color, brightness, effect and
        speed -- until it receives the mic-power-off frame. There is no dedicated
        "exit" opcode; the vendor app's mic toggle sends 7E 04 07 00 FF FF FF 00
        EF, and the strip then accepts the next normal frame. We only send it
        when we believe mic mode is active, so ordinary commands aren't doubled.
        A short settle lets the firmware finish switching modes before the
        caller's frame lands (sent too quickly, the strip drops it and stays
        stuck -- the symptom this fixes).
        """
        if not self.state.mic_enabled:
            return
        LOGGER.debug("%s: Exiting mic/music mode before normal command", self.name)
        # Raw write (not the decorated set_* path) so the outer @retry that wraps
        # the calling intent covers it without nesting.
        await self.transport.write_frame(ElkProtocol.mic_power(False))
        self.state.mic_enabled = False
        self.transport.fire_callbacks()
        await asyncio.sleep(MIC_EXIT_SETTLE)

    # ---- intents ----
    @retry_bluetooth_connection_error
    async def set_color_temp(self, value: int) -> None:
        value = max(0, min(int(value), 100))
        warm = value
        cold = 100 - value
        color_temp_cmd = self.protocol.model.get_color_temp_cmd(
            self.protocol.model_name, warm, cold
        )
        await self.transport.write_frame(color_temp_cmd)
        self.state.color_temp = warm
        self.state.effect = None

    @retry_bluetooth_connection_error
    async def set_color_temp_kelvin(self, value: int, brightness: int) -> None:
        # White colours are represented by a percentage from 0x00 (warm) to
        # 0x64 (cool), with a blend of the two channels between those values.
        await self._exit_mic_mode()
        min_temp = self.protocol.model.get_min_color_temp_kelvin(self.protocol.model_name)
        max_temp = self.protocol.model.get_max_color_temp_kelvin(self.protocol.model_name)
        value = max(value, min_temp)
        value = min(value, max_temp)

        # Ensure brightness is not None before using it
        if brightness is None:
            brightness = self.state.brightness if self.state.brightness is not None else 255

        # Clamp t to [0, 1]: 0 = warmest (min_temp), 1 = coolest (max_temp)
        t = (value - min_temp) / (max_temp - min_temp) if max_temp > min_temp else 1.0

        # Convert kelvin -> warm/cold percentages (0-100):
        # warm=100/cold=0 at min_temp (warmest), warm=0/cold=100 at max_temp (coolest)
        warm_pct = int((1.0 - t) * 100)
        cold_pct = int(t * 100)
        # Built once; doubles as the capability probe (empty -> RGB fallback below).
        cmd = self.protocol.model.get_color_temp_cmd(self.protocol.model_name, warm_pct, cold_pct)
        if cmd:
            # Set brightness via white channel if model supports it. Send both as
            # one atomic operation so the pair can't be interleaved by another
            # command on the same device (an empty white_cmd is skipped).
            white_cmd = self.protocol.model.get_white_cmd(self.protocol.model_name, brightness)
            await self.transport.write_frames(cmd, white_cmd)
            # Cache only after the write succeeds, and brightness only if it was
            # actually transmitted (no white command -> nothing applied it).
            self.state.color_temp_kelvin = value
            self.state.effect = None
            if white_cmd:
                self.state.brightness = brightness
            else:
                LOGGER.debug(
                    "%s: Model has no white command; brightness not sent with color temp", self.name
                )
            return

        # Fallback: RGB emulation for models without native color_temp command
        warm = (255, 138, 18)  # Warm white ~1800K
        cool = (180, 220, 255)  # Cool white ~7000K

        r = int(warm[0] + (cool[0] - warm[0]) * t)
        g = int(warm[1] + (cool[1] - warm[1]) * t)
        b = int(warm[2] + (cool[2] - warm[2]) * t)

        # Apply brightness scaling
        scale = brightness / 255.0
        r_scaled, g_scaled, b_scaled = int(r * scale), int(g * scale), int(b * scale)

        # Write directly (not via the @retry-decorated set_color) so this method's
        # own @retry doesn't nest with set_color's; cache only after success.
        color_cmd = self.protocol.model.get_color_cmd(
            self.protocol.model_name, r_scaled, g_scaled, b_scaled
        )
        await self.transport.write_frame(color_cmd)
        # RGB fallback DECOUPLES base from rgb (raw field writes, not apply_color):
        # rgb_color=scaled, rgb_color_base=unscaled interpolated white (§8.3).
        self.state.rgb_color = (r_scaled, g_scaled, b_scaled)
        self.state.effect = None
        self.state.rgb_color_base = (r, g, b)  # unscaled base for future brightness changes
        self.state.color_temp_kelvin = value
        self.state.brightness = brightness

    @retry_bluetooth_connection_error
    async def set_color(self, rgb: tuple[int, int, int], is_base_color: bool = False) -> None:
        await self._exit_mic_mode()
        r, g, b = rgb
        color_cmd = self.protocol.model.get_color_cmd(self.protocol.model_name, r, g, b)
        if not color_cmd:
            # White/temp-only models have no color command; don't pollute the
            # cached RGB state with a write that never happened.
            raise UnsupportedCommandError(f"{self.name}: model has no RGB color command")
        await self.transport.write_frame(color_cmd)
        self.state.rgb_color = rgb
        self.state.effect = None
        # If this is a base color (not brightness-scaled), save it
        if is_base_color:
            self.state.rgb_color_base = rgb

    @retry_bluetooth_connection_error
    async def set_white(self, intensity: int) -> None:
        await self._exit_mic_mode()
        if intensity is None:
            intensity = 255
        intensity = max(1, min(int(intensity), 255))
        white_cmd = self.protocol.model.get_white_cmd(self.protocol.model_name, intensity)
        await self.transport.write_frame(white_cmd)
        self.state.brightness = intensity
        self.state.effect = None

    @retry_bluetooth_connection_error
    async def set_brightness(self, intensity: int) -> None:
        """Set brightness with configurable mode (auto/rgb/native)."""
        await self._exit_mic_mode()
        value = max(1, min(int(intensity), 255))
        percent = round(value * 100 / 255)  # logging only; commands scale internally
        mode = (self._brightness_mode or "auto").lower()

        # ALWAYS scale from the base RGB color (not the already-scaled color) to
        # avoid cumulative scaling.
        r, g, b = self.state.rgb_color_base
        # Built once: the native-mode payload and the auto-mode capability probe.
        # get_brightness_cmd converts 0-255 -> 0-100 internally (same as
        # get_white_cmd); passing an already-converted percent double-scales.
        native_cmd = self.protocol.model.get_brightness_cmd(self.protocol.model_name, value)
        has_rgb = bool(self.protocol.model.get_color_cmd(self.protocol.model_name, 255, 255, 255))

        async def write_rgb_scaled():
            """Scale RGB from the base color and write it directly (not via the
            @retry-decorated set_color) to avoid nested retry amplification."""
            scale = value / 255.0
            rr, gg, bb = int(r * scale), int(g * scale), int(b * scale)
            color_cmd = self.protocol.model.get_color_cmd(self.protocol.model_name, rr, gg, bb)
            await self.transport.write_frame(color_cmd)
            self.state.rgb_color = (rr, gg, bb)  # scaled; the base color is preserved
            self.state.effect = None
            LOGGER.debug(
                "%s: Brightness set via RGB scaling: %d%% (Base RGB: %d,%d,%d -> Scaled: %d,%d,%d)",
                self.name,
                percent,
                r,
                g,
                b,
                rr,
                gg,
                bb,
            )

        async def write_native():
            """Send the model's native brightness command."""
            await self.transport.write_frame(native_cmd)
            LOGGER.debug("%s: Brightness set via native command: %d%%", self.name, percent)

        # NOTE: exceptions are intentionally NOT swallowed here -- they propagate
        # to @retry_bluetooth_connection_error so a transient BLE failure is
        # retried instead of being logged and falsely reported to HA as success.
        if mode == "rgb":
            if not has_rgb:
                raise UnsupportedCommandError(f"{self.name}: model has no RGB brightness command")
            await write_rgb_scaled()
        elif mode == "native":
            if not native_cmd:
                raise UnsupportedCommandError(
                    f"{self.name}: model has no native brightness command"
                )
            await write_native()
        # Prefer native, but fall back to RGB scaling when the model has no
        # native brightness command (an empty command is a silent no-op, not
        # an exception) or when the native write fails.
        elif native_cmd:
            try:
                await write_native()
            except Exception as e:
                if not has_rgb:
                    raise
                LOGGER.warning("%s: Native brightness failed, fallback to RGB: %s", self.name, e)
                await write_rgb_scaled()
        elif has_rgb:
            await write_rgb_scaled()
        else:
            raise UnsupportedCommandError(f"{self.name}: model has no brightness command")
        # Cache only after a successful write so a failed command never leaves
        # the reported brightness ahead of the device.
        self.state.brightness = value

    @retry_bluetooth_connection_error
    async def set_effect_speed(self, value: int) -> None:
        # Device speed is a 0-100 percent; clamp so an out-of-range value (e.g. a
        # restored 0-255 value from before this range fix) isn't sent verbatim.
        value = max(0, min(int(value), 100))
        effect_speed = self.protocol.model.get_effect_speed_cmd(self.protocol.model_name, value)
        await self.transport.write_frame(effect_speed)
        self.state.effect_speed = value

    @retry_bluetooth_connection_error
    async def set_effect(self, value: int) -> None:
        await self._exit_mic_mode()
        effect = self.protocol.model.get_effect_cmd(self.protocol.model_name, value)
        await self.transport.write_frame(effect)
        self.state.effect = value

    @retry_bluetooth_connection_error
    async def set_mic_effect(self, value: int) -> None:
        """Set the microphone EQ mode (music-reactive).

        ELK/DOM strips expose 4 EQ modes (0x80-0x83); only MELK/MODELX widen to
        8 (0x80-0x87). The vendor app clamps the value to the model's range, so
        we do the same instead of sending an out-of-range byte to a DOM strip.
        Selecting an EQ puts the strip in music mode, so we record that here; the
        next normal command then knows to send an explicit mic-off first.
        """
        # Clamp folded into protocol.mic_eq (extended range for MELK/MODELX); the
        # emitted frame is byte-identical to the former inline literal.
        frame = self.protocol.mic_eq(
            value, extended=self.protocol.mic_effect_extended(self.transport.name)
        )
        await self.transport.write_frame(frame)
        self.state.mic_effect = frame[3]
        self.state.mic_enabled = True
        self.transport.fire_callbacks()
        LOGGER.debug("Mic effect set to: 0x%02x", frame[3])

    @retry_bluetooth_connection_error
    async def set_mic_sensitivity(self, value: int) -> None:
        """Set microphone sensitivity (0-100)."""
        if not 0 <= value <= 100:
            raise UnsupportedCommandError(
                f"{self.name}: microphone sensitivity must be between 0 and 100"
            )
        await self.transport.write_frame(self.protocol.mic_sensitivity(value))
        self.state.mic_sensitivity = value
        LOGGER.debug("Mic sensitivity set to: %d", value)

    @retry_bluetooth_connection_error
    async def enable_mic(self) -> None:
        """Enable external microphone."""
        await self.transport.write_frame(ElkProtocol.mic_power(True))
        self.state.mic_enabled = True
        self.transport.fire_callbacks()
        LOGGER.debug("External microphone enabled")

    @retry_bluetooth_connection_error
    async def disable_mic(self) -> None:
        """Disable external microphone (manual exit from music mode).

        Same frame as the automatic exit in _exit_mic_mode; the settle keeps a
        color/effect command issued right after the toggle from arriving before
        the strip has left music mode.
        """
        await self.transport.write_frame(ElkProtocol.mic_power(False))
        self.state.mic_enabled = False
        self.transport.fire_callbacks()
        await asyncio.sleep(MIC_EXIT_SETTLE)
        LOGGER.debug("External microphone disabled")

    @retry_bluetooth_connection_error
    async def turn_on(self) -> None:
        cmd = self.protocol.model.get_turn_on_cmd(self.protocol.model_name)
        await self.transport.write_frame(cmd)
        self.state.is_on = True

    @retry_bluetooth_connection_error
    async def turn_off(self) -> None:
        cmd = self.protocol.model.get_turn_off_cmd(self.protocol.model_name)
        await self.transport.write_frame(cmd)
        self.state.is_on = False

    @retry_bluetooth_connection_error
    async def set_scheduler_on(self, days: int, hours: int, minutes: int, enabled: bool) -> None:
        # byte[1]=0x08 (frame length), byte[6]=0x00 selects the ON timer,
        # byte[7] bit7 (0x80) = timer enabled. Frame built by protocol.scheduler.
        await self.transport.write_frame(
            self.protocol.scheduler(days, hours, minutes, enabled, off=False)
        )

    @retry_bluetooth_connection_error
    async def set_scheduler_off(self, days: int, hours: int, minutes: int, enabled: bool) -> None:
        # Same frame as the ON timer but byte[6]=0x01 selects the OFF timer.
        await self.transport.write_frame(
            self.protocol.scheduler(days, hours, minutes, enabled, off=True)
        )

    @retry_bluetooth_connection_error
    async def sync_time(self) -> None:
        now = dt_util.now()
        date = now.date()
        # The strip's weekday byte is Sunday-based (Sun=0 .. Sat=6), matching
        # the app's Calendar.DAY_OF_WEEK-1. isoweekday() is Mon=1..Sun=7, so
        # mod 7 maps Sun(7)->0 and leaves Mon..Sat as 1..6.
        day_of_week = date.isoweekday() % 7
        cmd = self.protocol.model.get_sync_time_cmd(
            self.protocol.model_name,
            int(now.strftime("%H")),
            int(now.strftime("%M")),
            int(now.strftime("%S")),
            day_of_week,
        )
        await self.transport.write_frame(cmd)

    @retry_bluetooth_connection_error
    async def custom_time(self, hour: int, minute: int, second: int, day_of_week: int) -> None:
        cmd = self.protocol.model.get_custom_time_cmd(
            self.protocol.model_name, hour, minute, second, day_of_week
        )
        await self.transport.write_frame(cmd)

    async def query_state(self) -> None:
        """Query device state using model-specific command."""
        # NOT @retry: guarded on a live connection; command routes through
        # transport.write_frame so the op-lock is held like every other write.
        if not self.transport.is_connected:
            return

        query_cmd = self.protocol.model.get_query_cmd(self.protocol.model_name)
        if query_cmd:
            try:
                LOGGER.debug("%s: Querying state with model command", self.name)
                # Route through write_frame so the operation lock is held (matches
                # every other command); _write_while_connected would bypass it.
                await self.transport.write_frame(query_cmd)
                await asyncio.sleep(0.2)
            except Exception as e:  # noqa: BLE001 - optional query must not block setup
                LOGGER.debug("%s: Query command failed: %s", self.name, e)

    @retry_bluetooth_connection_error
    async def update(self) -> None:
        # These controllers do not provide reliable readback, but a refresh must
        # still prove that a usable GATT connection and characteristics exist.
        # Let failures propagate to Home Assistant instead of publishing a false
        # successful refresh with an arbitrary OFF state.
        if self.state.is_on is None:
            self.state.is_on = False
            self.state.rgb_color = (0, 0, 0)
            self.state.color_temp_kelvin = 5000
            self.state.brightness = 255

        await self.transport.connect()
        self.transport.refresh_device_data()
