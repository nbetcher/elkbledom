# Selective upstream ports and effect-speed limits

Subsequent review found additional interaction defects. See
[review remediation and regression gates](review-remediation.md) for the fixes
and current validation; the 84-test results below identify this earlier snapshot.

Date: 2026-09-05. Base: `4368d4eaaf490061c9fbd6431444b6b0697fa419`.
Upstream reviewed: `f41b8a27838f5b76f2b8b2c628b61cd700fe5fc3`.

## Implemented ports

- Preserve the selected RGB color in HA while dimming, using the unscaled base
  rather than reconstructing it from rounded wire values. This adapts the useful
  part of upstream `ece1e91`; our fork already restored the base color correctly.
  The explicit reset-to-white path now updates that base too.
- Include the device name in BLE command logs, as in upstream PR #152. Existing
  GATT response selection, pacing, serialization, retry ownership, and proxy
  teardown behavior remain in place.
- Add upstream's iStrip+ compatibility pointer. This is documentation, not new
  device support or an additional integration dependency.

No upstream merge or cherry-pick was used. The fork's modular architecture and
existing protocol fixes are retained.

## Speed decision: retain 0–100, make limits model-specific

Upstream `fb36379` sets ELK-BLEDOB's limit to 183 and defaults other models to
255. Our `8785e9b` instead established 0–100 following an Elkotrol app-protocol
investigation. A merged upstream change is not sufficient evidence to undo a
model/protocol safeguard, especially when similarly named controllers differ.

Additional primary evidence checked:

| Evidence | Observation | Limitation |
| --- | --- | --- |
| [Upstream PR #152](https://github.com/dave-code-ruiz/elkbledom/pull/152) | Introduces the 183 cap and per-model range metadata. | No supporting captures or tests are attached to the PR. |
| [ELK-BLEDOB capture](https://github.com/8none1/elk-bledob/blob/f009236b2e786a6304572132c117df491124f5d3/wireshark_pcaps/effects.pcapng) | Decoding with tshark found 111 speed writes, spanning 0 through 100. | Captures app traffic for one controller variant; does not prove a universal firmware maximum. |
| [ELK-BLEDOM command implementation](https://github.com/user154lt/ELK-BLEDOM-Command-Util/blob/696c4c849b9daafd7e39524644b5c38cacbb97a3/CommandUtils.kt) | The speed builder clamps its argument to 0–100. | Independent controller implementation, not evidence for every model. |

Capture SHA-256:
`110d90ee2350e549d20db3b40183abd83ed561b7b38e89546dabc7389f8a1441`.
The speed sweep is in packets 43–153, with the minimum at packet 103 and maximum
at packet 152. Those frames use `7e 07 02 <speed> ff ff ff 00 ef`; the fork's
existing app-derived ELK frame uses `7e 04 02`. This is another reason not to
replace model-wide protocol bytes based on a shared advertised name.

To reproduce the capture extraction after downloading the pinned file:

```text
tshark -r effects.pcapng -Y "btatt.value[0:3] == 7e:07:02" -T fields -e frame.number -e btatt.value
```

Conclusion: the evidence supports preserving the conservative 0–100 range.
It does not establish that 101–183 is unsafe on every device, nor that 183 or
255 is safe on the user's controller. No higher-range hardware claim is made.

## Implementation and regression prevention

- `EffectSpeedLimits` is the shared validation/clamping policy. Model metadata
  may specify `effect_speed_range: {"min": 0, "max": 100}`. Bounds must be
  ordered integer bytes. All shipped models retain 0–100, and ELK-BLEDOB records
  that choice explicitly. Other models retain the existing conservative default;
  their maximum hardware capabilities have not been independently established.
- `Model.get_effect_speed_cmd` validates the range before encoding. It does not
  rescale a raw speed or silently change an invalid action request.
- Invalid, fractional, nonfinite, or out-of-range action values raise a translated
  HA validation error before Bluetooth I/O; failed writes cannot advance state.
- A fixed frame is not an adjustable command. `XSL-` and `LED LIGHT STRIP` have
  a literal speed byte of 187 with no variable placeholder. Their misleading
  sliders and automatic speed follow-up writes are suppressed; raw frame
  definitions are preserved, not guessed into new protocol support. Existing
  registry entries for those removed controls may remain unavailable until the
  user removes them; automations and registry records are not deleted.
- The number's limits, encoder validation, and restoration use the same model
  policy. Native number restoration uses HA's recommended
  [RestoreNumber API](https://developers.home-assistant.io/docs/core/entity/number/#restoring-number-states).
  A fallback reads older unitless records so the first upgrade preserves saved
  speed and microphone sensitivity. Display values with units are not mistaken
  for native values.
- Saved speed is clamped to current model limits without device I/O. The number's
  own saved value wins over the older light extra attribute regardless of which
  platform restores first.
- Regression tests cover low-brightness RGB rounding, reset-to-white, labelled
  command logs, all shipped ranges, unsupported fixed frames, model-specific
  bounds, invalid actions, failed writes, native/legacy restoration, invalid
  recorder values, and restoration order. A synthetic 60–183 model verifies
  the extension point only; it is not shipped hardware support.

## Requirements before widening a shipped range

Identify the precise controller/firmware and GATT layout; capture the controlling
app's speed commands and record the physical effect. Verify boundary values and
effect changes, then add only that model's metadata and regression cases. Keep
other models on their established ranges. Do not infer physical compatibility
from successful byte encoding or mocked tests.

No physical controller was commanded during this work. Full live
HA → ESPHome proxy → controller qualification remains a separate release gate.

## Final verification and candidate

- Home Assistant 2026.8.0 / Python 3.14.6: **84 tests passed**.
- Home Assistant 2026.9.0 / Python 3.14.6: **84 tests passed**.
- Ruff 0.13.1 lint and formatting: passed (31 Python files).
- Local hassfest: 1 integration, 0 invalid.
- Extracted candidate on HA 2026.9.0: all 19 component modules imported from
  the archive, and all 84 tests passed in an isolated temporary directory.
- Source runs report one upstream HA HTTP/aiohttp inheritance deprecation warning.
- Repository Issues/topics were read back as enabled/populated; no remote
  workflow or release was triggered by this work.

Candidate: `dist/elkbledom-2.0.17-upstream-speed.zip`, containing 43 verified
integration files. Every archived file was compared byte-for-byte to source.
SHA-256: `d2d1c9bc2898236dd3fd4c3df806ed2d632aa379d122bdca197ad5c9e07a7c40`.
The manifest remains 2.0.17: this is an unpublished local candidate. Prior ZIPs
were preserved, not overwritten. Source edits have not been committed or pushed
as part of this follow-up.
