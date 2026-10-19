# Style guide (template)

Copy this file into your project (for example `docs/style-guide.md`), fill in the placeholders and give it to every agent or editor as part of the brief. It is the pipeline's **memory**: whenever a revision is approved, write it down as a rule here so the same mistake is not repeated.

Everything in angle brackets is a placeholder. Numbers are sensible starting points, not requirements.

## 1. Brand

- Background: `<#FFFFFF>` · dark panel: `<#111111>` · ink: `<#111111>` · **one** accent: `<#FFC400>` (at most one accent element per frame).
- Typeface: `<font family, weight>` (ship the font file locally in the project).
- Material/texture: `<e.g. flat shapes, paper collage, line art, photography>`.
- Real assets: when the topic is a specific product, model or event, prefer official logos and screenshots of real pages over generated images. Crop to the relevant part, **remove personal names**, note source URL and capture date.
- Mirror the accent color in `kurgu.config.json` → `colors.accent` so captions and Shorts match.

## 2. Motion

- Pace: fast but readable. Most beats 0.8–2.0 s; every text or number stays on screen long enough to be read once.
- At least one short still moment per video; the last 1.2–1.5 s are still.
- Easing: soft (expo / power4 / sine in-out), slow continuous drift, no hard pops.
- Minimum readable text: ~40 px on 1920×1080, ~48 px on 1080×1920.
- Transitions between panels: iris, clip-path wipe, push, morph. A small carrier element (dot, line, shape) can travel through transitions.

## 3. Layout rules for talking-head + screen videos

- **TALK** (webcam full frame): cards go on the empty side of the frame, never over the face.
- **SCREEN** (screen recording full frame + round webcam PiP bottom-right): graphics use the top band or bottom-left, never the PiP corner.
- No made-up data. Illustrative charts have no numeric axis and are labelled "illustrative".

## 4. Technique library (avoid "same template every time")

Pick a varied mix per video; play with the footage itself, not only with cards on top of it.

| # | Technique | Idea |
|---|---|---|
| 01 | orbit | logos/cards orbit a central concept in 3D |
| 02 | screen wall | wall of screenshots, camera flies in and dives into one |
| 03 | ui world | real UI pieces float in depth layers |
| 04 | frame break | subject breaks out of a card or screen edge (mask) |
| 05 | digital zoom | hard punch-in on a key sentence |
| 06 | device mockup | screen recording enters a phone/laptop mockup |
| 07 | checklist | items arrive and get ticked |
| 08 | headline wall | real headlines/posts stack up (names removed) |
| 09 | zoom-through cut | dive into an element and exit into the next scene |
| 10 | chart grow | bars/lines grow with real data |
| 11 | pull-back reveal | start on a detail, pull back to context |
| 12 | background split | subject separated from background (segmentation) |
| 13 | paper cut | layered cut-paper collage, 12 fps |
| 14 | doodle boil | hand-drawn arrows/circles with line boil |
| 15 | vertigo | dolly zoom: subject fixed, background stretches |
| 16 | cover story | frozen frame becomes a magazine cover |
| 17 | glass type | large frosted/refractive typography |
| 18 | kinetic captions | word-by-word animated text |
| 19 | freeze frame | frame freezes, desaturates, gets annotated |
| 20 | speed ramp | speed up/slow down through waiting/loading |

Suggested rules: in a ~10 min video use at least 12 different techniques, no technique more than 3 times (kinetic captions excepted), never the same one twice in a row, and at least 4 that manipulate the footage itself (05, 11, 12, 15, 19, 20). Each line of the animation plan names its technique.

## 5. Sound

- A small, fixed SFX set reused in every video, for example: `whoosh` (panel change), `pop` (element in), `tick` (counter), `click` (step/button), `impact` (key result), `chime` (ending). Put the files in `sfx/library/`.
- SFX around −18…−22 dB under speech; never stack several at the same instant.
- Music bed under speech ≈ −33 LUFS with ducking (`music.speech_lufs`), final mix at `audio.target_lufs` (default −14 LUFS, ≤ −1 dBTP).

## 6. Acceptance checks

- ffprobe: size, 30 fps, full duration, clean full decode.
- Animation tool lint/check: 0 errors.
- Contact sheets (`qa/`) reviewed by eye: overflow, readability, face/PiP collisions, leftover frame edges.
- Revisions are produced as new versions (v2, v3); old versions are kept.

## 7. Revision log

Format: `date, project vN→vN+1: "what the reviewer said" → new rule`. Add one line per approved revision and update the sections above.

- `<YYYY-MM-DD>, <project> v1→v2: "<feedback>" → <rule>`
