# Vertical Shorts

Run from the project root. No network or extra packages needed.

```bash
python scripts/make_shorts.py --config edit/shorts.json [--settings kurgu.config.json]
```

`--config` is the Shorts plan ([`examples/shorts.json`](../examples/shorts.json)); `--settings` is the shared pipeline settings file. For a new video only the JSON changes: source/EDL/transcript/sync paths, Short ranges, crop/region and optional `talk_track` keyframes.

All times are **webcam times**. Ranges are intersected with the approved EDL; a start inside a word moves forward, an end inside a word moves back. Plans outside 35–60 s stop the run — list the sentence to drop in `exclude`.

- `--plan-only` checks duration, words and cuts without rendering.
- `--only 3,5` processes selected Shorts.
- `--self-check` runs offline unit checks.

Vertical pieces, ASS captions, output words and the time map are cached in `work/shorts/<id>/`. `base.mp4` is the captioned intermediate without hook/CTA; `voice-music.wav` is the PCM source for the final mix. Cache keys change when captions, font, framing or sync change.

## Captions and corrections

Word-timed ASS captions with 1–3 visible words; the active word uses `colors.accent` (override per project with a `colors` block in `shorts.json`). Width is measured with the font's TrueType metrics against `font.max_width`. If no 700-weight file exists, the 600 file is used for a 700 request (`requested_weight` / `available_weight`).

ASR fixes in `shorts.json`:
- `asr_rules` — regex rules, same schema as `timemap.py --corrections`
- `corrections` — word → word
- `scoped_corrections` — word → word within a webcam time range
- `phrase_corrections` — multi-word replacements
- `protect_phrases` — phrases the rules must not touch (e.g. a title like "Cloud City")

Output names are `<name_prefix>-<nn>-<id>.mp4` (default prefix `short`) plus a matching `.srt`.

## Audio

Webcam only, the same cleanup chain as the long video, 12 ms cut crossfades, two-pass loudnorm. A music excerpt from the body of the bed sits around `music.speech_lufs` with soft ducking (≤ 5 dB) and fades out over the last 1.2 s. Hook and CTA SFX are added at their configured gains. The final AAC must pass `audio.target_lufs ± audio.lufs_tolerance` and `audio.true_peak_dbtp`.

## Updating graphics only

```bash
python scripts/make_shorts.py --config edit/shorts.json --overlays-only
```

Re-composites transparent MOV graphics on the cached vertical base and rebuilds the mix from PCM. Missing graphics/SFX are skipped with a warning. If heavy edit settings changed, the cache mismatch stops the run; use the normal command.

`--qa-only` re-checks existing outputs: full decode, size/fps/duration, loudness, SPLIT audio correlation and eight labelled frames. `qa/shorts/<id>-qa.json` holds the measurements, `<id>-captions.txt` the caption text. Review the sheets by eye.

## Extra animation layer (v2)

A JSON keyed by Short id with `overlays` and `sfx` lists. Paths are relative to the project root, `start` is relative to the start of the Short (hook included). Pass it with `--extra-overlays` or `extra_overlays` in the config.

```bash
python scripts/make_shorts.py --config edit/shorts.json --overlays-only --extra-overlays edit/shorts-overlays.json --output-suffix -v2 --output-dir renders/shorts/v2
```

MP4 and SRT get the same suffix. If extra graphics would overwrite the v1 name/folder, the command stops before writing. Layer order: cached video → v2 animations → ASS captions → hook/CTA. Burned-in captions cannot be removed with `--overlays-only`; rebuild normally to change caption text or position.
