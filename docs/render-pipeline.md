# Base render and composite

Run from the video project root. Paths inside JSON files are relative to that folder. Sources are only read; writing into `raw/` is refused. No network access is needed.

## Base edit

```bash
python scripts/render_base.py --edl edit/final_edl.json --layout edit/layout.json --output renders/base.mp4 --work-dir work/segments
python scripts/render_base.py --edl edit/final_edl.json --layout edit/layout.json --output renders/base-preview.mp4 --work-dir work/segments-preview --preview
```

- The EDL is applied through `TimeMap`. Layout mode boundaries snap to the nearest source word start; zoom times are converted to output time.
- 30 fps frames are rounded once on the global timeline, so per-piece rounding never accumulates. Audio stays sample-accurate to the EDL.
- **TALK**: alternating `talk.zoom_levels` (default 1.00 / 1.12) at each cut, plus a slow push (`talk.push`, default +2 %) inside each piece. Face position comes from `face` in `layout.json`.
- **SCREEN**: `screen_crop` removes the recording's outer frame, aspect-preserving fill, round webcam PiP (`pip.diameter`, `margin`, `border`, soft shadow).
- Mode changes last `transition_s` (default 0.45 s) and morph the face between full frame and PiP using PyAV + NumPy frames.
- Optional zoom `keyframes` (`time` in webcam seconds, `center` in source pixels, `scale`) follow the recording's own zooms/scrolls with cosine easing.
- Pieces are encoded with NVENC (`video.nvenc_cq`) or libx264 (`video.x264_crf`) when NVENC is unavailable, then stream-copy concatenated. Finished pieces are cached by source, settings and code hash.
- Audio: webcam only. Stereo → mono, cut by EDL samples with an `audio.crossfade_s` linear crossfade at every cut, highpass 80 Hz, light `afftdn`, 3:1 compressor, optional de-esser. Two-pass loudnorm to `audio.target_lufs` with headroom below `audio.true_peak_dbtp` for AAC.
- `work/segments/render-plan.json` records modes/zooms and the source↔output mapping; `render-result.json` records codec and loudness data.

Screen sync comes from the plateaus in `work/sync.json`. Pieces switch at the midpoint of ambiguous transitions; no linear drift is applied to the whole video.

### Anchored zooms for small UI text near the bottom edge

Add `output_anchor: [0.5, 0.55]`, `mask_below: <y>` and optionally `background` to a zoom. `center` is the target in source pixels; the anchor moves it to the given position of the output frame. Area below the source is filled with `colors.screen_fill`, and a taskbar can be masked. Around 2.2× works well for prompt boxes.

## Overlays and final composite

```bash
python scripts/composite.py --input renders/base.mp4 --overlays edit/overlays.json --output renders/final.mp4 --work-dir work/composite
```

Schema: [`examples/overlays.json`](../examples/overlays.json).

- Overlay and SFX times are relative to the start of the base video. The total prepend length is added automatically; do not shift times yourself.
- `alpha` keeps ProRes 4444 transparency; `full` covers the frame. Overlay audio is ignored; effects come from `sfx`.
- Prepend/append clips keep their own audio (silent stereo is added when missing). A 30 ms fade-out is applied to the base before the append.
- Music loops; with `duck: true` the base speech is the sidechain key. Default gains: `audio.sfx_gain_db`, `audio.music_gain_db`.
- Missing overlay/SFX/intro/outro/music files are skipped with a warning; broken media and ffmpeg errors stop the run.
- With empty lists the base is copied without re-encoding. `--preview` renders 960×540 with a fast preset.
- ProRes inputs are opened in 30 s windows; each base frame is encoded once, windows are stream-copied together, SFX/music are mixed in PCM and AAC-encoded once after a two-pass loudnorm.
- `work/composite/composite-result.json` records the shift, duration, warnings and the files found in `--sfx-library`.

## Checks

```bash
python scripts/check_render.py      # project root, needs raw/webcam.mp4 + edit/*.json
python scripts/qa_base.py --video renders/base.mp4 --edl edit/final_edl.json --layout edit/layout.json --plan work/segments/render-plan.json --qa-dir qa
python scripts/qa_final.py --video renders/final.mp4 --base renders/base.mp4 --overlays edit/overlays.json --plan work/segments/render-plan.json --manifest work/composite/composite-result.json --qa-dir qa/final
```

- `check_render.py` tests planning against your project plus synthetic clips: alpha/full overlays, prepend/append with and without audio, automatic time shift, music ducking, missing-file skips, decodable output.
- `qa_base.py` checks format/duration, full decode, loudness, screen↔base audio correlation at SCREEN points and black frames, and writes a labelled contact sheet (`qa/base-contact.jpg`) around every mode change, zoom and a sample of cuts.
- `qa_final.py` decodes the whole video, checks format/duration/loudness, builds contact sheets of every overlay (+ intro/outro) and of chapter bumpers (ids `ch1`, `ch2`, … — change with `--chapter-prefix`), compares transparent areas with the base, measures 400 ms momentary loudness around selected SFX (`--sfx-check id …`, default three spread across the video) and records sample jumps at audio seams. Review the sheets by eye.

Adding music to an approved render (video stream-copied): [music bed](music-bed.md).
