# Prompt: edit decision

Use with any capable LLM (or as a checklist for a human editor). Attach the files listed under **Inputs**. Replace angle-bracket placeholders.

---

You are the editor of a talking-head + screen-recording video. The recording is in <language>. Your job is to decide what to cut and how to frame each moment. Do not invent content; only remove and arrange.

**Inputs**
- `work/transcript.json` — word-level transcript (webcam time, seconds).
- `work/edl.json` — automatic cut suggestions from `cutlist.py`, including the `review` list of uncertain spots.
- `docs/style-guide.md` — style rules.

**Rules**
1. Keep every word whose removal would change meaning. Never cut inside a word.
2. Remove: abandoned sentences, restarts (keep the last clean take), talk about the recording itself ("let me start over", "we'll cut this"), long dictation pauses, false starts.
3. Keep emphatic repeats ("very very"), jokes and asides that carry personality.
4. The video starts at the first clean greeting and ends <closing_handle_s> s after the sign-off.
5. Resolve every item in `review`: cut or keep, one line of reasoning each.

**Output 1 — `edit/manual_cuts.json`** (schema: `examples/manual_cuts.json`)
For each cut: `id`, `start`, `end` (webcam seconds), `reason`, `text` (exact removed words), `expected_removed_word_indices`. Add a `protect` interval for the closing handle.

**Output 2 — `edit/layout.json` `modes` and `zooms`** (schema: `examples/layout.json`)
- `TALK` while only talking, `SCREEN` while the screen matters. Mode starts at sentence starts.
- A `zoom` wherever small UI text must be readable (target region centre, scale 1.5–2.2).

**Output 3 — short report**
Original vs. expected duration, number of cuts by reason, any spot you were unsure about.
