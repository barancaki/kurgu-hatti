# Music bed

Add a music track under an approved render in one command. The picture is not re-encoded.

```bash
python scripts/mix_music.py --input renders/final.mp4 --music music/bed.mp3 --output renders/final-music.mp4 --work-dir work --intro 5 --outro 4
```

Set `--intro` / `--outro` to the length of your intro and outro. A separate `--work-dir` keeps earlier music intermediates. Outputs that collide with an input are refused. The input should already be at the target loudness (`audio.target_lufs`).

With a clean speech key (no SFX) the ducking is more precise:

```bash
python scripts/mix_music.py --input renders/final.mp4 --music music/bed.mp3 --output renders/final-music.mp4 --work-dir work --intro 5 --outro 4 --voice-key work/segments/normalized-calibrated.wav --key-start 5 --bpm-hint 95
```

Without `--voice-key` the speech band of the input is the key, so SFX can trigger ducking too. `--key-start` defaults to the intro length.

How it works:

- `music_bed.py` finds tempo and 4/4 bar phase from a spectral onset envelope, FFT autocorrelation and energy changes. Automatic bar phase is not a substitute for a score.
- The source intro is used once at the start and its native outro once at the end. The body uses different bar entries matched by chroma and energy, with fade handles inside the body.
- Joins are 1.5–2 s equal-power crossfades with energy-bump compensation; consecutive section energy steps are limited to 2 dB.
- The nearest whole-bar layout is fitted to the video length with at most 3.5 % pitch-preserving tempo change. Targets too short for the native intro/outro are refused.
- After ducking/fades the music alone is calibrated to `music.title_lufs` in intro/outro and `music.speech_lufs` under speech (override with `--title-lufs` / `--speech-lufs`).
- Ducking: ratio 3, −24 dBFS RMS threshold, 30 ms attack, 400 ms release, at most 5 dB extra reduction.
- Fade-in 0.4 s, fade-out 3 s; speech/SFX bus stays at 0 dB, no new global loudnorm. If the first AAC attempt exceeds the peak ceiling only peak limiting is applied. Video packets are stream-copied and their SHA-256 equality is verified.
- Writes `music-bed.wav/json`, `music-bus.wav`, `music-mix.json` and `music-ducking.json` into the work dir.

Music bed only:

```bash
python scripts/music_bed.py --music music/bed.mp3 --duration 600.0 --output work/music-bed.wav --json work/music-bed.json --bpm-hint 95
```

Checks:

```bash
python scripts/check_music.py
python scripts/qa_music.py --input renders/final.mp4 --video renders/final-music.mp4 --mix-report work/music-mix.json --qa-dir qa/music
```

`qa/music/crossfade-NN.wav` holds ±3 s of music around each join; PNGs show waveform and log-frequency spectrum with the crossfade marked. `music-qa.json` records all numeric checks.
