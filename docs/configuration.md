# Configuration

All scripts share one settings file. Resolution order:

1. `--config PATH` (`--settings PATH` for `make_shorts.py`, whose `--config` is the Shorts plan)
2. the `KURGU_CONFIG` environment variable
3. `./kurgu.config.json` in the current folder
4. built-in defaults (`scripts/settings.py`)

The file only needs the keys you change; sections are merged with the defaults. Start from [`kurgu.config.example.json`](../kurgu.config.example.json).

Per-video data stays in per-video files: `edit/layout.json` (layout modes, crop, face position, PiP size, zooms), `edit/manual_cuts.json`, `edit/overlays.json`, `edit/shorts.json`.

## Keys

| Key | Default | Used by | Meaning |
|---|---|---|---|
| `language` | `"tr"` | transcribe, cutlist, finalize_edl | Whisper language code and lexicon preset (`tr`, `en`) |
| `lexicon.*` | preset | cutlist, finalize_edl | Override any lexicon field (below) |
| `transcribe.model` | `"large-v3"` | transcribe | faster-whisper model name or local folder |
| `transcribe.beam_size` | `5` | transcribe | Beam size |
| `transcribe.cpu_threads` | `8` | transcribe | Threads for the CPU fallback |
| `cuts.silence_gap_s` | `0.45` | cutlist | Silences longer than this are shortened |
| `cuts.breath_s` | `0.20` | cutlist | Quiet kept inside a shortened silence |
| `cuts.filler_min_s` | `0.40` | finalize_edl | Wordless voiced audio at least this long is cut as a filler |
| `cuts.filler_padding_s` | `[0.10, 0.14]` | finalize_edl | Margin kept around filler and manual cuts |
| `cuts.min_keep_s` | `0.25` | finalize_edl | Shorter wordless keeps are removed |
| `cuts.closing_handle_s` | `0.60` | (your manual `protect`) | Suggested tail after the last word |
| `audio.target_lufs` | `-14` | render_base, composite, make_shorts, QA | Integrated loudness of deliverables |
| `audio.true_peak_dbtp` | `-1` | same | True-peak ceiling |
| `audio.lufs_tolerance` | `0.5` | QA | Accepted ± around the target |
| `audio.crossfade_s` | `0.012` | render_base (if `layout.audio.crossfade_s` is unset), make_shorts | Crossfade at every cut |
| `audio.sfx_gain_db` | `-18` | composite | Default SFX gain when an entry has none |
| `audio.music_gain_db` | `-26` | composite | Default music gain |
| `music.speech_lufs` | `-33` | mix_music | Music level under speech after ducking |
| `music.title_lufs` | `-20` | mix_music | Music level in intro/outro |
| `video.nvenc_cq` / `video.x264_crf` | `19` / `18` | all encoders | Final quality |
| `video.preview_nvenc_cq` / `video.preview_x264_crf` | `24` / `23` | `--preview` | Preview quality |
| `colors.accent` | `"0xFFC400"` | make_shorts | Active caption word, divider dot |
| `colors.divider` | `"0x0B0B0B"` | make_shorts | SPLIT divider |
| `colors.screen_fill` | `"0x111410"` | render_base, make_shorts | Fill behind anchored screen zooms |
| `colors.contact_sheet` | `"0x171717"` | make_shorts | QA sheet background |

PiP size, TALK zoom levels, transition length and crossfade per video live in `layout.json` (see [`examples/layout.json`](../examples/layout.json)).

## Lexicon fields

| Field | Meaning |
|---|---|
| `initial_prompt` | Whisper prompt that encourages it to write fillers |
| `turkish_casing` | Map `I→ı`, `İ→i` before lowercasing |
| `filler_regex` | Full-match regex for pure filler tokens (cut automatically) |
| `safe_repeat_words` | Words whose immediate doubling is trimmed automatically |
| `emphatic_repeat_words` | Doublings that must survive ("very very") |
| `ambiguous_fillers` | Words that are sometimes fillers; sent to review, never auto-cut |
| `ambiguous_keep_before` / `ambiguous_keep_after` | Context words that mark the ambiguous word as meaningful |
| `retake_phrases` | Phrases that flag talk about the recording ("cut that", "start again") |

The `tr` preset is the one the pipeline was tuned and tested on. The `en` preset is a starting point; check `work/edl.json` review items on your first video and adjust.
