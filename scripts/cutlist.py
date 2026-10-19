"""Suggest safe webcam-time cuts using word alignment and 5ms RMS measurements."""
import argparse
import json
from pathlib import Path
import re

import numpy as np
from scipy import ndimage

from common import audio, join_words, output_path, rms_frames, write_json
import settings


def norm(text, turkish_casing=True):
    if turkish_casing:
        text = text.replace('I', 'ı').replace('İ', 'i')
    return re.sub(r'[\W_]', '', text.lower())


def pure_filler(text, lex):
    return bool(re.fullmatch(lex['filler_regex'], norm(text, lex['turkish_casing'])))


UNTRANSCRIBED = '[No transcribed words; audio energy present]'


def runs(mask):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))


def combine(candidates):
    merged = []
    for item in sorted(candidates, key=lambda x: (x['start'], x['end'])):
        if item['end'] <= item['start']:
            continue
        if merged and item['start'] < merged[-1]['end']:
            last = merged[-1]
            last['end'] = max(last['end'], item['end'])
            last['sources'].append(item)
            if item['reason'] != 'silence':
                last['reason'] = item['reason']
            last['text'] = ' '.join(x['text'] for x in last['sources'] if x['text'])
        else:
            merged.append(dict(item, sources=[dict(item)]))
    return merged


def analyze(transcript, samples, sr, gap=.45, breath=.2, lex=None):
    lex = lex or settings.lexicon('tr')
    turkish = lex['turkish_casing']
    duration = float(transcript['duration'])
    words = sorted([dict(w) for s in transcript['segments'] for w in s['words']
                    if w['word'].strip()], key=lambda w: (w['start'], w['end']))
    if not words:
        raise ValueError('Transcript has no words; refusing to cut untranscribed audio')
    if any(not 0 <= w['start'] <= w['end'] <= duration + .001 for w in words):
        raise ValueError('Word timestamps outside source duration')
    rms, hop = rms_frames(samples, sr)
    nonzero = rms[rms > 1e-7]
    threshold = max(1e-4, float(np.percentile(nonzero, 10)) * 3) if len(nonzero) else 1e-4
    # Bridge very brief low-energy phonemes before looking for silence.
    active = ndimage.maximum_filter1d(rms, size=max(1, round(.025 / hop))) > threshold

    def snap(t, lo, hi):
        left = max(0, int(max(lo, t - .02) / hop))
        right = min(len(rms), int(min(hi, t + .02) / hop) + 1)
        if right <= left:
            return min(hi, max(lo, t))
        values = rms[left:right]
        minima = np.flatnonzero(values == values.min()) + left
        index = int(minima[np.argmin(abs((minima + .5) * hop - t))])
        return min(hi, max(lo, (index + .5) * hop))

    candidates, review = [], []

    def add_review(start, end, text, reason, kind):
        if end > start:
            review.append({'start': float(start), 'end': float(end), 'text': text,
                           'reason': reason, 'kind': kind})

    def removable_word(first, last, reason):
        before = words[first - 1]['end'] if first else 0.
        after = words[last + 1]['start'] if last + 1 < len(words) else duration
        start = words[first]['start']
        end = words[last]['end']
        if (words[first]['probability'] < .65 or end <= start or
                start - before < .025 or after - end < .025):
            add_review(start, end, join_words(words[first:last + 1]),
                       'Word boundary, confidence or gap to neighbouring speech is not safe for an automatic cut.', reason)
            return
        left = snap(start - .015, max(before + .015, start - .04), start)
        right = snap(end + .015, end, min(after - .015, end + .04))
        candidates.append({'start': left, 'end': right, 'reason': reason,
                           'text': ' '.join(w['word'].strip() for w in words[first:last + 1])})

    # Silence is eligible only outside all aligned word intervals.
    coverage = []
    for word in words:
        if coverage and word['start'] <= coverage[-1][1]:
            coverage[-1][1] = max(coverage[-1][1], word['end'])
        else:
            coverage.append([word['start'], word['end']])
    gaps = list(zip([0.] + [b for a, b in coverage], [a for a, b in coverage] + [duration]))
    for start, end in gaps:
        if end - start <= gap:
            continue
        lo, hi = max(0, int(np.ceil(start / hop))), min(len(active), int(end / hop))
        for a, b in runs(~active[lo:hi]):
            quiet_start = max(start, (lo + a) * hop)
            quiet_end = min(end, (lo + b) * hop)
            if quiet_end - quiet_start <= gap:
                continue
            half = breath / 2
            left = snap(quiet_start + half, quiet_start + half - .025, quiet_start + half + .025)
            right = snap(quiet_end - half, quiet_end - half - .025, quiet_end - half + .025)
            if right > left:
                candidates.append({'start': left, 'end': right, 'reason': 'silence', 'text': '',
                                   'gap_start': start, 'gap_end': end,
                                   'retained_quiet_s': (left - quiet_start) + (quiet_end - right)})
        # Significant unaligned energy may be omitted hesitation or real speech.
        energy_duration = float(np.sum(active[lo:hi]) * hop)
        if energy_duration > gap:
            add_review(start, end, UNTRANSCRIBED,
                       f'{energy_duration:.2f}s above the silence threshold: filler, breath or missed speech. Voiced part was not cut automatically.',
                       'untranscribed_audio')
    normalized = [norm(w['word'], turkish) for w in words]
    safe_repeat = set(lex['safe_repeat_words'])
    ambiguous = set(lex['ambiguous_fillers'])
    keep_before, keep_after = set(lex['ambiguous_keep_before']), set(lex['ambiguous_keep_after'])
    for i, word in enumerate(words):
        token = normalized[i]
        if pure_filler(word['word'], lex):
            removable_word(i, i, 'filler')
        elif token in ambiguous:
            previous = normalized[i - 1] if i else ''
            following = normalized[i + 1] if i + 1 < len(words) else ''
            if previous in keep_before or following in keep_after:
                continue
            # Lexical context cannot prove an ambiguous word is a filler; human review decides.
            add_review(word['start'], word['end'], ' '.join(w['word'].strip() for w in words[max(0, i-4):i+5]),
                       f'"{token}" may be a filler; not cut automatically to protect meaningful use.', 'filler')
        if i + 1 < len(words) and token.isalpha() and token == normalized[i + 1] and words[i+1]['start'] - word['end'] <= .6:
            if token in safe_repeat and word['end'] - word['start'] <= .8:
                removable_word(i, i, 'repetition')
            else:
                add_review(word['start'], words[i+1]['end'], word['word'] + ' ' + words[i+1]['word'],
                           'Consecutive repeat may be emphasis or a meaningful doubling.', 'repetition')
    # Match word sequences, including a restart embedded inside a Whisper segment.
    seen_restarts = set()
    for i in range(len(words) - 2):
        if not all(t.isalpha() for t in normalized[i:i+3]):
            continue
        for j in range(i + 3, len(words) - 2):
            if words[j]['start'] - words[i]['start'] > 15:
                break
            if normalized[i:i+3] != normalized[j:j+3]:
                continue
            if i and normalized[i-1] == normalized[j-1]:
                continue
            if len(set(normalized[i:i+3])) < 2:
                continue
            key = (round(words[i]['start'], 2), round(words[j]['start'], 2))
            if key in seen_restarts:
                continue
            seen_restarts.add(key)
            earlier = ' '.join(w['word'].strip() for w in words[i:min(j, i+12)])
            later = ' '.join(w['word'].strip() for w in words[j:j+8])
            add_review(words[i]['start'], words[j]['start'], earlier + ' → ' + later,
                       'Same phrase/sentence start repeats within 15s: abandoned take, emphasis or clarification.', 'restart')
    segments = transcript['segments']
    for segment in segments:
        text = (segment['text'].replace('I', 'ı') if turkish else segment['text']).lower()
        if any(t in text for t in lex['retake_phrases']):
            add_review(segment['start'], segment['end'], segment['text'].strip(),
                       'Talk about the recording/edit; review together with the previous abandoned take.', 'recording_note')

    # Long quiet spans inside a stretched ASR word need alignment review before cutting.
    for a, b in runs(~active):
        start, end = a * hop, b * hop
        if end - start <= .8:
            continue
        overlapping = [w for w in words if w['start'] < end and w['end'] > start]
        if overlapping and any(min(end, w['end']) - max(start, w['start']) > .3 for w in overlapping):
            add_review(start, end, ' '.join(w['word'].strip() for w in overlapping),
                       'RMS shows silence but an ASR word interval covers it; fix the boundary by listening.',
                       'alignment_pause')
    for word in words:
        if word['end'] <= word['start']:
            add_review(max(0, word['start'] - .05), min(duration, word['start'] + .05), word['word'].strip(),
                       'ASR produced a zero-length word; real speech and word boundary are uncertain.', 'asr_alignment')
    for i in range(len(words) - 3):
        if (normalized[i:i+2] == normalized[i+2:i+4] and
                all(t.isalpha() for t in normalized[i:i+2]) and
                words[i+2]['start'] - words[i+1]['end'] < 1 and
                words[i+2]['start'] - words[i]['start'] < 4):
            add_review(words[i]['start'], words[i+3]['end'],
                       ' '.join(w['word'].strip() for w in words[i:i+4]),
                       'The same two-word phrase repeats back to back; listen to tell emphasis from a stumble.',
                       'phrase_repetition')

    cuts = combine(candidates)
    keeps = []
    cursor = 0.
    for cut in cuts + [{'start': duration, 'end': duration}]:
        if cut['start'] > cursor:
            text = join_words([w for w in words
                               if cursor <= (w['start'] + w['end']) / 2 < cut['start']])
            keeps.append({'start': cursor, 'end': cut['start'], 'text': text})
        cursor = cut['end']
    result_duration = sum(k['end'] - k['start'] for k in keeps)
    assert all(0 <= k['start'] < k['end'] <= duration for k in keeps)
    assert all(a['end'] <= b['start'] for a, b in zip(keeps, keeps[1:]))
    assert abs(result_duration + sum(c['end'] - c['start'] for c in cuts) - duration) < 1e-6
    stats = {}
    for reason in ['silence', 'filler', 'repetition']:
        matching = [c for c in cuts if c['reason'] == reason]
        stats[reason] = {'count': len(matching), 'removed_s': sum(c['end'] - c['start'] for c in matching)}
    return {'timeline': 'webcam seconds, half-open intervals [start,end)',
            'raw_duration_s': duration, 'proposed_duration_s': result_duration,
            'removed_duration_s': duration - result_duration, 'cut_count': len(cuts),
            'counts': stats, 'parameters': {'silence_gap_s': gap, 'retained_breath_s': breath,
                                          'rms_frame_s': hop, 'rms_silence_threshold': threshold},
            'keep': keeps, 'cuts': cuts, 'review': sorted(review, key=lambda r: (r['start'], r['end'])),
            'edge_regions': {'before_first_word': {'start': 0., 'end': words[0]['start']},
                             'after_last_word': {'start': words[-1]['end'], 'end': duration}},
            'limitations': ['ASR may omit filler sounds despite prompting; energy without aligned words is preserved for review.',
                           'Phrase repetitions/restarts and ambiguous fillers require listening.',
                           'RMS is not a semantic voice detector; breathing/clicks may remain.',
                           'This is an analysis EDL; no source media was modified or video rendered.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transcript', required=True)
    parser.add_argument('--audio', required=True, help='Webcam/master audio on transcript timeline')
    parser.add_argument('--output', required=True)
    parser.add_argument('--silence-gap', type=float, help='Override cuts.silence_gap_s')
    parser.add_argument('--breath', type=float, help='Override cuts.breath_s')
    settings.add_config_arg(parser)
    args = parser.parse_args()
    cfg = settings.load(args.config)
    gap = cfg['cuts']['silence_gap_s'] if args.silence_gap is None else args.silence_gap
    breath = cfg['cuts']['breath_s'] if args.breath is None else args.breath
    if gap <= 0 or not .05 <= breath <= gap - .05:
        parser.error('silence gap must be positive and breath must be in [0.05, gap - 0.05]')
    out = output_path(args.output, [args.transcript, args.audio])
    transcript = json.loads(Path(args.transcript).read_text(encoding='utf-8'))
    samples, sr = audio(args.audio)
    if abs(len(samples)/sr - transcript.get('audio_duration', transcript['duration'])) > .1:
        raise ValueError('Audio duration does not match transcript; verify the webcam timeline')
    result = analyze(transcript, samples, sr, gap, breath, cfg['lexicon'])
    write_json(out, result)
    print(json.dumps({k: result[k] for k in ['raw_duration_s', 'proposed_duration_s', 'counts', 'cut_count']}, indent=2))


if __name__ == '__main__':
    main()
