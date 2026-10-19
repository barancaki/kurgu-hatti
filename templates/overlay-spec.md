# <Video title> — animation plan (template)

Filled in per video (by you or an AI editor) and handed to whoever builds the animations. Rows below are illustrative.

Video: <language>, ~<length> min, 1920×1080, 30 fps. Topic: <one sentence>.
Layouts: **TALK** (webcam full frame; speaker on the <left/right>, empty side → cards) and **SCREEN** (screen recording full frame + 300 px webcam circle bottom-right, 40 px margin → keep that corner clear).

## Delivery
- One composition per element, 1920×1080, 30 fps, **transparent** → ProRes 4444 with alpha, `overlays/<id>.mov`. Only `intro` / `outro` are opaque full-frame MP4.
- Times below are **webcam (raw) times**. Convert to edited time with `scripts/timemap.py --to-output`; word timings are in `edit/out_words.json`, text in `edit/out_transcript.txt`.
- Finish with `edit/overlays.json` (schema: `examples/overlays.json`). Chapter bumpers use ids `ch1`, `ch2`, … so `qa_final.py` can check them.

## Style
Follow `docs/style-guide.md` (from `templates/style-guide.md`).

## Elements (webcam time; quoted speech = trigger)
| id | Webcam time | Layout | Technique | Visual | Content |
|---|---|---|---|---|---|
| intro | prepended, ~5 s | full frame | 09 zoom-through cut | generated illustration | Cold open: an object breaks apart into the title; iris opens onto the first frame |
| questions | 00:28.5–00:40.3 | TALK | 07 checklist | none | Three question cards drop on the empty side, each as it is spoken (pop) |
| ch1 | 04:36.0, 1.4 s | transition | 09 zoom-through cut | none | Chapter bumper "01 · <chapter>", covers the TALK→SCREEN change (whoosh) |
| counter | 04:46.2–04:53.1 | SCREEN | 10 chart grow | real screenshot | Counter rises to the real number in the top band (tick) |
| freeze | 05:48.9–06:12.2 | SCREEN | 19 freeze frame | none | Frame freezes and desaturates, arrow + label drawn on top |
| outro | appended, ~4 s | full frame | — | none | "Thanks for watching" + "Link in the description"; last 1.4 s still (chime) |

## QA
- Every composition lints with 0 errors; take a mid-frame snapshot of each overlay and check overflow, readability and face/PiP collisions.
- Once `renders/base.mp4` exists, composite a few overlays onto the matching base frame and look.
