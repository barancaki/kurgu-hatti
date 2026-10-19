# <Video title> — Shorts plan (template)

Source: the approved long edit (`edit/final_edl.json`). Shorts are **re-framed vertically** from the raw sources with the same sync and cuts; they are not cropped from the 16:9 render.

## Format
1080×1920, 30 fps, H.264 + AAC 48 kHz, 35–60 s, speech at `audio.target_lufs`, music bed ≈ −33 LUFS with soft ducking, 1.2 s fade at the end.

## Layouts
- **TALK-V**: face-centred 9:16 crop of the webcam; alternate 1.00↔1.10 zoom at cuts.
- **SPLIT**: top 1080×1100 screen recording cropped to the relevant region, bottom 1080×820 face, thin divider with an accent dot.
- **Captions (required)**: word-timed, 1–3 words, ~76 px, white with black outline, active word in `colors.accent`. Safe area: 220 px top, 380 px bottom, 140 px right.

## Graphics
- `hook-<n>.mov` (2.8 s) two-line hook card at the top; `cta.mov` (2.2 s) "Full video on the channel".
- 3–5 content-synced animations per Short in `edit/shorts-overlays.json`, one main element at a time, ≥ 48 px, never over face or captions.

## Shorts
| # | id | Hook | Ranges (webcam) | Layout | Animations |
|---|---|---|---|---|---|
| 1 | demo-screen | "Hook line / second line" | 02:00.0 → 02:50.0 (end of sentence) | SPLIT | chip, list, diagram |
| 2 | demo-talk | "Big question / here" | 05:00.0 → 05:52.0 | TALK-V | illustrative curve, quote card |

- Over 60 s: drop the weakest sentence with `exclude` and report it.
- Every Short starts and ends on a full sentence; first word within 0.15 s.
- Output: `renders/shorts/<name_prefix>-<nn>-<id>.mp4` + matching `.srt`.

`edit/shorts-overlays.json` (times from the start of the Short, hook included):
```json
{"demo-screen": {"overlays": [{"file": "overlays/shorts/v2/demo-screen/01-chip.mov", "start": 2.9}],
                 "sfx": [{"file": "sfx/library/pop.mp3", "start": 3.0, "gain_db": -20}]}}
```
