# Contributing

Thanks for helping. Issues and pull requests in English or Turkish are welcome. By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).

## Ground rules

- **Never commit recordings or personal data.** No raw footage, renders, transcripts of real people, API keys, `.env` files or absolute paths from your machine. `.gitignore` covers the usual folders; check `git status` before committing.
- **Keep scripts standalone.** Each script must run with plain Python + ffmpeg and its CLI arguments. AI agents are an optional layer on top; do not make a script depend on a specific model or service.
- **Settings over constants.** New thresholds, levels or colors go into `scripts/settings.py` (`DEFAULTS`) and `docs/configuration.md`, not into script bodies. Per-video choices belong in the project's JSON files.
- **Offline and safe by default.** Scripts must not download anything, must refuse to write into `raw/`, and must not overwrite their inputs (use `common.output_path`).
- **Conservative cuts.** When the pipeline is not sure, it adds an item to the review list instead of cutting.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

ffmpeg/ffprobe must be on `PATH`.

## Before opening a pull request

```bash
python scripts/check_analysis.py
python scripts/check_final_edit.py
python scripts/check_music.py
python scripts/make_shorts.py --self-check
python -m unittest discover -s tools -p "test_*.py"
# every CLI script answers --help (check_analysis.py and check_render.py are test runners without it)
for f in scripts/*.py; do
  case "$f" in *check_analysis.py|*check_render.py) continue ;; esac
  python "$f" --help > /dev/null || echo "FAIL $f"
done
```

If you have a real project, also run `scripts/check_render.py` in its root and look at the contact sheets.

## Adding a language

1. Add a preset to `LEXICONS` in `scripts/settings.py` (filler regex, safe repeats, emphatic repeats, ambiguous fillers, retake phrases, Whisper prompt).
2. Add a small assertion to `check_analysis.py` (like the `tr` / `en` filler checks).
3. Mention it in `docs/configuration.md`.

## Style

- Match the surrounding code: short functions, `pathlib`, JSON written with `common.write_json`.
- User-facing messages and docs in English; keep Turkish in `README.tr.md`.
- One logical change per pull request, with a short description of what you tested.
