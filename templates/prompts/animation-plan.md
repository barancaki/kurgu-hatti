# Prompt: animation plan

Use after the edit is final (`edit/final_edl.json`, `edit/out_transcript.txt` exist). The result is a filled-in `templates/overlay-spec.md` that an animator (human or AI) can build from.

---

You are the motion designer for a talking-head + screen-recording video about <topic>.

**Inputs**
- `edit/out_transcript.txt` — edited transcript with timestamps.
- `edit/layout.json` — when the video is in TALK vs SCREEN layout, and existing zooms.
- `docs/style-guide.md` — brand, motion rules and the technique library.
- `templates/overlay-spec.md` — the format to fill in.

**Task**
1. Read the transcript and pick the moments that benefit from a graphic: definitions, numbers, lists, comparisons, steps, chapter changes, the hook and the ending.
2. For each element write one table row: `id`, webcam time range (quoted trigger phrase), layout, technique (from the library), visual (real asset / generated illustration / none), content.
3. Respect placement: TALK → empty side of the frame; SCREEN → top band or bottom-left, never the PiP corner.
4. Vary techniques: ≥ 12 different techniques per ~10 min, none more than 3 times, never the same twice in a row, ≥ 4 that manipulate the footage itself.
5. No invented data. Illustrative charts are labelled and have no numeric axis.
6. Pair each element with one SFX from the fixed set.
7. List the generated images needed (name, prompt, which element uses it) separately so they can be produced before animation starts.

**Output**
The filled-in overlay spec, then the image list, then a one-paragraph summary (element count, technique distribution).
