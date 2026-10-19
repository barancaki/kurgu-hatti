"""Offline self-checks for offset signs, subtitle limits and safe EDL coverage."""
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
from scipy import signal

from common import output_path
from cutlist import analyze, pure_filler
import settings
from sync import match, residual_lag
from transcribe import subtitles


def word(start, end, text, probability=.99):
    return dict(start=start, end=end, word=text, probability=probability)


def self_check():
    tr, en = settings.lexicon('tr'), settings.lexicon('en')
    assert pure_filler('Eee,', tr) and pure_filler('Im', tr) and not pure_filler('şey', tr)
    assert pure_filler('Umm...', en) and pure_filler('uh', en) and not pure_filler("I'm", en)
    assert settings.load()['cuts']['silence_gap_s'] == .45
    rng = np.random.default_rng(42)
    speech = rng.normal(size=8000)
    later = np.r_[np.zeros(2400), speech]
    earlier = np.r_[speech, np.zeros(2400)]
    for a, b, expected in [(later, earlier, .3), (earlier, later, -.3)]:
        c = signal.correlate(a - a.mean(), b - b.mean(), method='fft')
        lag = signal.correlation_lags(len(a), len(b))[np.argmax(c)] / 8000
        assert abs(lag - expected) < 1 / 8000
        residual, score = residual_lag(a, b, 8000, limit=.4)
        assert abs(residual - expected) < 1 / 8000 and score > .99
    index, score, _ = match(speech, np.r_[np.zeros(701), speech, np.zeros(500)])
    assert index == 701 and score > .999

    sr = 16000
    samples = np.zeros(sr * 6, dtype='float32')
    words = [word(.5, .8, 'bu'), word(.9, 1.2, 'bu'), word(1.5, 1.7, 'bir'),
             word(1.8, 2.1, 'şey'), word(4., 4.3, 'eee'), word(4.5, 5., 'bitti.')]
    for w in words:
        samples[round(w['start'] * sr):round(w['end'] * sr)] = .03
    transcript = {'duration': 6., 'segments': [{'start': .5, 'end': 5., 'text': 'deneme', 'words': words}]}
    edl = analyze(transcript, samples, sr)
    assert any(c['reason'] == 'repetition' for c in edl['cuts'])
    assert any(c['reason'] == 'filler' for c in edl['cuts'])
    assert not any(c['start'] < 2.1 and c['end'] > 1.8 for c in edl['cuts']), 'Meaningful bir şey must remain'
    for w in [words[1], words[2], words[3], words[5]]:
        assert not any(c['start'] < w['end'] and c['end'] > w['start'] for c in edl['cuts'])
    assert abs(sum(k['end']-k['start'] for k in edl['keep']) +
               sum(c['end']-c['start'] for c in edl['cuts']) - 6) < 1e-9
    low = json.loads(json.dumps(transcript))
    low['segments'][0]['words'][4]['probability'] = .2
    assert any(r['kind'] == 'filler' for r in analyze(low, samples, sr)['review'])
    caps = subtitles([{'words': [word(i * .3, i * .3 + .25, 'mühendislik') for i in range(30)]}])
    for cue in caps.strip().split('\n\n'):
        lines = cue.splitlines()[2:]
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines)
    try:
        output_path('raw/do-not-write.json')
        raise AssertionError('raw guard failed')
    except ValueError:
        pass
    for name in ['transcribe', 'sync', 'cutlist']:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name + '.py')), '--help'],
                                capture_output=True)
        assert result.returncode == 0, (name, result.stderr)
    print('PASS: positive/negative sync sign, normalized FFT, safe word cuts, meaningful şey, low-confidence review, complete interval coverage, SRT limits, raw protection, three --help commands')


def validate_artifacts(folder):
    transcript = json.loads((folder / 'transcript.json').read_text(encoding='utf-8'))
    edl = json.loads((folder / 'edl.json').read_text(encoding='utf-8'))
    sync = json.loads((folder / 'sync.json').read_text(encoding='utf-8'))
    duration = transcript['duration']
    assert edl['raw_duration_s'] == duration
    for field in ['keep', 'cuts']:
        intervals = edl[field]
        assert all(0 <= x['start'] < x['end'] <= duration for x in intervals)
        assert all(a['end'] <= b['start'] for a, b in zip(intervals, intervals[1:]))
    partition = sorted(edl['keep'] + edl['cuts'], key=lambda x: x['start'])
    assert partition[0]['start'] == 0 and abs(partition[-1]['end'] - duration) < 1e-8
    assert all(abs(a['end'] - b['start']) < 1e-8 for a, b in zip(partition, partition[1:]))
    assert abs(sum(x['end']-x['start'] for x in edl['keep']) - edl['proposed_duration_s']) < 1e-8
    words = [w for s in transcript['segments'] for w in s['words']]
    assert all(0 <= w['start'] <= w['end'] <= duration and 0 <= w['probability'] <= 1 for w in words)
    assert sync['sign_verification']['passed']
    assert len(sync['measurements']) >= 1 and all(m['window_s'] > 0 for m in sync['measurements'])
    assert sync['within_40ms'] or (sync['drift_note'] and sync['local_drift'])
    for cue in (folder / 'webcam.srt').read_text(encoding='utf-8').strip().split('\n\n'):
        lines = cue.splitlines()
        assert len(lines[2:]) <= 2 and all(len(t) <= 42 for t in lines[2:])
    review = (folder / 'edit-review.md').read_text(encoding='utf-8')
    assert len(review.splitlines()) <= 250
    print(f'PASS: artifacts, {len(words)} words, {len(edl["keep"])} keep intervals, {len(edl["cuts"])} cuts, {len(review.splitlines())} report lines')


if __name__ == '__main__':
    self_check()
    if len(sys.argv) == 2:
        validate_artifacts(Path(sys.argv[1]))
