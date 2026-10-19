"""Small offline regression checks plus validation of final editing artifacts."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

from finalize_edl import finalize
from timemap import TimeMap, correct_text, format_time, source_words, word_membership


def self_check():
    mapping = TimeMap([{'start': 2., 'end': 4.}, {'start': 7., 'end': 10.}], 12.)
    assert mapping.duration == 5
    assert [mapping.to_output(t) for t in [0, 2, 3, 4, 5, 7, 9, 10, 12]] == [0, 0, 1, 2, 2, 2, 4, 5, 5]
    assert [mapping.to_webcam(t) for t in [0, 1, 2, 4, 5]] == [2, 3, 7, 9, 10]
    for t in np.linspace(0, 5, 31):
        assert abs(mapping.to_output(mapping.to_webcam(t)) - t) < 1e-9
    for bad in [-1, 13, float('nan'), float('inf')]:
        try:
            mapping.to_output(bad)
            raise AssertionError('Invalid time accepted')
        except ValueError:
            pass
    try:
        TimeMap([{'start': 2, 'end': 4}, {'start': 3, 'end': 5}], 8)
        raise AssertionError('Overlapping keep accepted')
    except ValueError:
        pass
    try:
        word_membership([{'start': 1, 'end': 3, 'word': 'yarım'}], [{'start': 2, 'end': 4}])
        raise AssertionError('Partial word accepted')
    except ValueError:
        pass
    assert format_time(59.999) == '01:00.00'
    rules = [{'pattern': r'\bCloud\b(?!\s+City\b)', 'replacement': 'Claude'},
             {'pattern': r'\bChatGP\b', 'replacement': 'ChatGPT'},
             {'pattern': r'\bhisse\b', 'replacement': 'iş', 'start': 73, 'end': 78}]
    corrected = correct_text("Cloud'a Cloud City ChatGP hisse", 73.58, rules)
    for expected in ["Claude'a", 'Cloud City', 'ChatGPT', 'iş']:
        assert expected in corrected, expected
    assert correct_text('hisse', 111, rules) == 'hisse'
    assert correct_text('Cloud', 0) == 'Cloud'

    sr = 16000
    samples = np.zeros(8 * sr, dtype='float32')
    intervals = [(.3, .8, 'başla'), (1.4, 1.8, 'çok'), (1.9, 2.3, 'çok'),
                 (3., 3.5, 'çıkar'), (4., 4.5, 'devam'), (6., 6.4, 'veda'), (7., 7.5, 'kapat')]
    words = [dict(start=a, end=b, word=t, probability=.99) for a, b, t in intervals]
    for w in words:
        samples[round(w['start']*sr):round(w['end']*sr)] = .03
    samples[round(4.5*sr):round(6*sr)] = .02
    transcript = {'duration': 8., 'audio_duration': 8., 'segments': [{'words': words}]}
    edl = {'raw_duration_s': 8., 'parameters': {'rms_silence_threshold': .001},
           'cuts': [{'start': .9, 'end': 1.25, 'reason': 'silence'},
                    {'start': 6.55, 'end': 6.9, 'reason': 'silence'}],
           'review': [{'start': 4.5, 'end': 6., 'kind': 'untranscribed_audio'}]}
    manual = {'protect': [{'start': 6.4, 'end': 7.}],
              'cuts': [{'id': 1, 'start': 3., 'end': 3.5},
                       {'id': 2, 'start': 7., 'end': 8.}]}
    result = finalize(edl, transcript, samples, sr, manual)
    assert result['validation']['word_boundary_violations'] == 0
    assert result['validation']['manual_cuts_applied'] == 2
    assert result['validation']['protected_emphatic_word_count'] == 2
    assert result['validation']['last_word']['word'] == 'veda'
    assert result['keep'][-1]['end'] >= 7.-1e-9
    assert any(c['start'] <= 4.64 and c['end'] >= 5.86 for c in result['cuts'])
    assert all(k['end']-k['start'] >= .25-1e-9 for k in result['keep'])
    for name in ['finalize_edl', 'timemap']:
        run = subprocess.run([sys.executable, str(Path(__file__).with_name(name+'.py')), '--help'], capture_output=True)
        assert run.returncode == 0, run.stderr
    print('PASS: cut snapping, inverse jump direction, mapping bounds, partial-word rejection, contextual text corrections, filler margins, emphatic repeats, closing protection, CLI --help')


def validate(edl_path, transcript_path, analysis_path, manual_path, folder, expect_text=()):
    read = lambda p: json.loads(Path(p).read_text(encoding='utf-8'))
    final, transcript, analysis, manual = map(read, [edl_path, transcript_path, analysis_path, manual_path])
    outputs = read(folder/'out_words.json')
    points = read(folder/'cut_points.json')
    words = source_words(transcript)
    keep, cuts = final['keep'], final['cuts']
    total = sum(k['end']-k['start'] for k in keep)
    assert abs(total-final['output_duration_s']) < 1e-8
    assert all(0 <= k['start'] < k['end'] <= transcript['duration'] for k in keep)
    assert all(a['end'] < b['start'] for a, b in zip(keep, keep[1:]))
    assert all(k['end']-k['start'] >= .25-1e-9 for k in keep)
    partition = sorted(keep+cuts, key=lambda x:x['start'])
    assert partition[0]['start'] == 0 and partition[-1]['end'] == transcript['duration']
    assert all(abs(a['end']-b['start']) < 1e-8 for a, b in zip(partition, partition[1:]))
    retained = {w['source_word_index'] for w in outputs['words']}
    expected_removed = {i for c in manual['cuts'] for i in c['expected_removed_word_indices']}
    assert retained == set(range(len(words)))-expected_removed, 'Unexpected retained/lost source word'
    actual_violations = [(w['source_word_index'], b) for w in words for k in keep
                         for b in [k['start'], k['end']] if w['start']+1e-8 < b < w['end']-1e-8]
    assert not actual_violations, actual_violations
    assert {m['id'] for m in final['manual_verification']} == {m['id'] for m in manual['cuts']}
    for item in final['manual_verification']:
        assert item['status'] == 'OK' and item['before_text'] and item['after_text']
        assert not retained.intersection(item['removed_word_indices'])
    for p in manual['protect']:
        assert any(k['start'] <= p['start'] and k['end'] >= p['end'] for k in keep)
    for cut in analysis['cuts']:
        # The only allowed automatic-cut override is the explicit protected handle.
        edges = sorted({cut['start'], cut['end']} |
                       {max(cut['start'], min(cut['end'], t)) for p in manual['protect'] for t in [p['start'], p['end']]})
        for a, b in zip(edges, edges[1:]):
            center = (a+b)/2
            if any(p['start'] <= center < p['end'] for p in manual['protect']):
                continue
            assert any(c['start'] <= a+1e-8 and c['end'] >= b-1e-8 for c in cuts), cut
    for filler in final['untranscribed_filler_audit']:
        assert .10-1e-8 <= filler['padding_left_s'] <= .14+1e-8
        assert .10-1e-8 <= filler['padding_right_s'] <= .14+1e-8
        for piece in filler['applied_cut_pieces']:
            assert any(c['start'] <= piece['start']+1e-8 and c['end'] >= piece['end']-1e-8 for c in cuts)
    for word in outputs['words']:
        assert abs((word['end']-word['start'])-(word['webcam_end']-word['webcam_start'])) < 1e-8
        k = keep[word['keep_index']]
        assert abs(word['start']-(k['output_start']+word['webcam_start']-k['start'])) < 1e-8
        assert 0 <= word['start'] < word['end'] <= total
    assert len(points['cut_points']) == len(keep)-1
    assert points['output_times'] == [k['output_start'] for k in keep[1:]]
    for p, before, after in zip(points['cut_points'], keep, keep[1:]):
        assert p['webcam_before'] == before['end'] and p['webcam_after'] == after['start']
    text = (folder/'out_transcript.txt').read_text(encoding='utf-8')
    for expected in expect_text:
        assert expected in text, expected
    assert final['validation']['first_word']['source_word_index'] == min(retained)
    assert final['validation']['last_word']['source_word_index'] == max(retained)
    assert abs(keep[-1]['end']-outputs['words'][-1]['webcam_end']-.6) < 1/16000
    print(f'PASS: {len(manual["cuts"])} manual edits, {len(outputs["words"])} complete words, 0 word-boundary violations, '
          f'{len(points["cut_points"])} jumps, {total:.6f}s, all source silence coverage except approved closing handle')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edl')
    parser.add_argument('--transcript')
    parser.add_argument('--analysis')
    parser.add_argument('--manual-cuts')
    parser.add_argument('--output-dir')
    parser.add_argument('--expect-text', nargs='*', default=[],
                        help='Phrases that must appear in out_transcript.txt (e.g. corrected product names)')
    args = parser.parse_args()
    self_check()
    if args.edl:
        if not all([args.transcript, args.analysis, args.manual_cuts, args.output_dir]):
            parser.error('Artifact validation requires all five path arguments')
        validate(args.edl, args.transcript, args.analysis, args.manual_cuts, Path(args.output_dir), args.expect_text)
