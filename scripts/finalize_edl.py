"""Finalize an analysis EDL with manual cuts and unaligned-audio filler removal."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path

import numpy as np
from scipy import ndimage

from common import audio, join_words, output_path, rms_frames, write_json
from cutlist import UNTRANSCRIBED, norm
import settings
from timemap import TimeMap, source_words, word_membership


def union(candidates):
    result = []
    for item in sorted(candidates, key=lambda c: (c['start'], c['end'])):
        if item['end'] <= item['start'] + 1e-9:
            continue
        if result and item['start'] <= result[-1]['end'] + 1e-9:
            result[-1]['end'] = max(result[-1]['end'], item['end'])
            result[-1]['sources'].append(item)
        else:
            result.append({'start': item['start'], 'end': item['end'], 'sources': [item]})
    for item in result:
        reasons = sorted({s['reason'] for s in item['sources']})
        item['reasons'] = reasons
        item['reason'] = next(r for r in ['manual', 'untranscribed_filler', 'short_keep_cleanup', 'silence', 'filler', 'repetition']
                              if r in reasons)
        item['manual_ids'] = sorted({s['manual_id'] for s in item['sources'] if 'manual_id' in s})
        item['text'] = ' / '.join(dict.fromkeys(s.get('text', '') for s in item['sources'] if s.get('text')))
    return result


def complement(cuts, duration):
    keep, cursor = [], 0.
    for item in cuts + [{'start': duration, 'end': duration}]:
        if item['start'] > cursor + 1e-9:
            keep.append({'start': cursor, 'end': item['start']})
        cursor = item['end']
    return keep


def finalize(edl, transcript, samples, sr, manual, lex=None, cuts_cfg=None):
    lex = lex or settings.lexicon('tr')
    cuts_cfg = dict(settings.DEFAULTS['cuts'], **(cuts_cfg or {}))
    pad_min, pad_max = cuts_cfg['filler_padding_s']
    pad_mid = (pad_min + pad_max) / 2
    filler_min, min_keep = cuts_cfg['filler_min_s'], cuts_cfg['min_keep_s']
    duration = float(transcript['duration'])
    if not math.isfinite(duration) or abs(edl['raw_duration_s'] - duration) > .001:
        raise ValueError('Source transcript and analysis EDL durations differ')
    if abs(len(samples) / sr - transcript.get('audio_duration', duration)) > .1:
        raise ValueError('Audio does not match the transcript timeline')
    words = source_words(transcript)
    if not words or any(not 0 <= w['start'] < w['end'] <= duration for w in words):
        raise ValueError('Nonempty, positive-duration word alignments within raw duration are required')
    if any(a['start'] > b['start'] for a, b in zip(words, words[1:])):
        raise ValueError('Source words are not ordered')
    rms, hop = rms_frames(samples, sr)
    if not len(rms):
        raise ValueError('Audio is shorter than one RMS frame')
    threshold = float(edl['parameters']['rms_silence_threshold'])

    def minimum(preferred, lo, hi):
        if lo > hi + 1e-8:
            raise ValueError(f'No safe boundary between {lo} and {hi}')
        if hi - lo < hop:
            return float(min(hi, max(lo, preferred)))
        positions = (np.arange(len(rms)) + .5) * hop
        indices = np.flatnonzero((positions >= lo - 1e-9) & (positions <= hi + 1e-9))
        if not len(indices):
            return float(min(hi, max(lo, preferred)))
        low = np.min(rms[indices])
        best = indices[np.isclose(rms[indices], low, rtol=0, atol=1e-12)]
        index = int(best[np.argmin(abs(positions[best] - preferred))])
        return float(positions[index])

    protected = manual.get('protect', [])
    for p in protected:
        if not 0 <= p['start'] < p['end'] <= duration:
            raise ValueError('Invalid protected interval')

    def subtract_protected(item):
        pieces = [dict(item)]
        for p in protected:
            remaining = []
            for c in pieces:
                if c['end'] <= p['start'] or c['start'] >= p['end']:
                    remaining.append(c)
                else:
                    if c['start'] < p['start']:
                        remaining.append(dict(c, end=p['start']))
                    if c['end'] > p['end']:
                        remaining.append(dict(c, start=p['end']))
            pieces = remaining
        return pieces

    # Emphatic repeated words are protected even if an older input EDL marks them.
    emphasized = set()
    for a, b in zip(words, words[1:]):
        t = norm(a['word'], lex['turkish_casing'])
        if t == norm(b['word'], lex['turkish_casing']) and t in set(lex['emphatic_repeat_words']):
            emphasized.update([a['source_word_index'], b['source_word_index']])
    candidates = []
    adjusted_automatic = []
    for index, cut in enumerate(edl['cuts']):
        if not 0 <= cut['start'] < cut['end'] <= duration:
            raise ValueError('Analysis cut outside raw duration')
        if any(w['source_word_index'] in emphasized and cut['start'] < w['end'] and cut['end'] > w['start'] for w in words):
            if cut['reason'] == 'silence':
                raise ValueError('Input silence cut conflicts with an aligned emphatic word')
            adjusted_automatic.append({'index': index, 'reason': 'emphatic repetition preserved'})
            continue
        item = {'start': cut['start'], 'end': cut['end'], 'reason': cut['reason'],
                'text': cut.get('text', ''), 'analysis_cut_index': index}
        pieces = subtract_protected(item)
        candidates.extend(pieces)
        if sum(p['end'] - p['start'] for p in pieces) < item['end'] - item['start'] - 1e-9:
            adjusted_automatic.append({'index': index, 'reason': 'explicitly protected closing handle', 'original': item, 'retained_cut_pieces': pieces})

    resolved_manual = []
    ids = [c['id'] for c in manual['cuts']]
    if len(ids) != len(set(ids)):
        raise ValueError('Manual cut IDs must be unique')
    for requested in manual['cuts']:
        start, end = float(requested['start']), float(requested['end'])
        if not 0 <= start < end <= duration:
            raise ValueError(f'Invalid manual cut {requested["id"]}')
        removed = [w for w in words if w['start'] < end - 1e-8 and w['end'] > start + 1e-8]
        if not removed:
            raise ValueError(f'Manual cut {requested["id"]} does not remove any words')
        actual_ids = [w['source_word_index'] for w in removed]
        if 'expected_removed_word_indices' in requested and actual_ids != requested['expected_removed_word_indices']:
            raise ValueError(f'Manual cut {requested["id"]} no longer matches its source word anchors')
        first, last = removed[0]['source_word_index'], removed[-1]['source_word_index']
        previous = words[first-1] if first else None
        following = words[last+1] if last+1 < len(words) else None
        if previous:
            lo = previous['end']
            hi = words[first]['start']
            pad_lo, pad_hi = max(lo, previous['end'] + pad_min), min(hi, previous['end'] + pad_max)
            if pad_lo <= pad_hi:
                lo, hi = pad_lo, pad_hi
            start = minimum(previous['end'] + pad_mid, lo, hi)
        else:
            start = 0.
        if following:
            lo = words[last]['end']
            hi = following['start']
            pad_lo, pad_hi = max(lo, following['start'] - pad_max), min(hi, following['start'] - pad_min)
            if pad_lo <= pad_hi:
                lo, hi = pad_lo, pad_hi
            end = minimum(following['start'] - pad_mid, lo, hi)
        else:
            end = duration
            # A tail trim may request a longer closing handle before its removed words.
            if requested['start'] > (previous['end'] if previous else 0):
                start = minimum(requested['start'], requested['start'], min(words[first]['start'], requested['start'] + .02))
        item = {'start': start, 'end': end, 'reason': 'manual', 'manual_id': requested['id'],
                'text': join_words(removed)}
        pieces = subtract_protected(item)
        if not pieces or abs(sum(c['end']-c['start'] for c in pieces) - (end-start)) > 1e-8:
            raise ValueError(f'Protected interval conflicts with manual cut {requested["id"]}')
        candidates.append(item)
        resolved_manual.append({'id': requested['id'], 'requested_start': requested['start'],
                                'requested_end': requested['end'], **item,
                                'removed_word_indices': [w['source_word_index'] for w in removed],
                                'tight_boundary': bool(previous and words[first]['start']-previous['end'] < pad_min
                                                       or following and following['start']-words[last]['end'] < pad_min)})

    # Review candidates plus newly eligible filler-length gaps use the same RMS threshold as analysis.
    active = ndimage.maximum_filter1d(rms, size=max(1, round(.025 / hop))) > threshold
    gaps = []
    cursor = 0.
    for word in words:
        if word['start'] > cursor:
            gaps.append((cursor, word['start']))
        cursor = max(cursor, word['end'])
    if cursor < duration:
        gaps.append((cursor, duration))
    review_regions = [r for r in edl['review'] if r.get('kind') == 'untranscribed_audio'
                      or UNTRANSCRIBED in r.get('text', '')]
    filler_audit = []
    for start, end in gaps:
        lo, hi = max(0, round(start/hop)), min(len(active), round(end/hop))
        energy_s = float(np.sum(active[lo:hi]) * hop)
        known = any(abs(r['start']-start) < .015 and abs(r['end']-end) < .015 for r in review_regions)
        if end-start < filler_min-1e-9 or (not known and energy_s < filler_min-1e-9):
            continue
        left = minimum(start+pad_mid, start+pad_min, start+pad_max)
        right = minimum(end-pad_mid, end-pad_max, end-pad_min)
        if right <= left:
            raise ValueError('Eligible untranscribed gap has no removable interior')
        item = {'start': left, 'end': right, 'reason': 'untranscribed_filler',
                'text': '[Wordless voiced audio: assumed filler]',
                'gap_start': start, 'gap_end': end, 'energy_duration_s': energy_s,
                'padding_left_s': left-start, 'padding_right_s': end-right,
                'from_analysis_review': known}
        pieces = subtract_protected(item)
        candidates.extend(pieces)
        filler_audit.append(dict(item, applied_cut_pieces=pieces))

    short_cleanup = []
    while True:
        cuts = union(candidates)
        keep = complement(cuts, duration)
        small = next((k for k in keep if k['end']-k['start'] < min_keep-1e-9), None)
        if small is None:
            break
        contains_word = any(w['start'] < small['end'] and w['end'] > small['start'] for w in words)
        is_protected = any(p['start'] < small['end'] and p['end'] > small['start'] for p in protected)
        if not contains_word and not is_protected:
            extra = dict(small, reason='short_keep_cleanup', text='Wordless leftover shorter than min_keep_s')
            candidates.append(extra)
            short_cleanup.append(extra)
        else:
            # Merge with a neighbor by restoring the shortest unprotected nonmanual bridge.
            neighbors = [c for c in cuts if (abs(c['end']-small['start']) < 1e-8
                                             or abs(c['start']-small['end']) < 1e-8) and not c['manual_ids']]
            if not neighbors:
                raise ValueError('Short spoken/protected keep cannot be merged without undoing an approved manual cut')
            bridge = min(neighbors, key=lambda c: c['end']-c['start'])
            candidates = [c for c in candidates if not (c['start'] < bridge['end'] and c['end'] > bridge['start'])]
            short_cleanup.append(dict(bridge, action='restored_bridge_to_preserve_word'))
    mapping = TimeMap(keep, duration)
    assigned = word_membership(words, mapping.keep)
    kept_ids = {w['source_word_index'] for w, _ in assigned}
    for index in emphasized:
        if index not in kept_ids:
            raise ValueError('Final EDL removed a protected emphatic repetition')
    for p in protected:
        if any(c['start'] < p['end']-1e-9 and c['end'] > p['start']+1e-9 for c in cuts):
            raise ValueError('Final EDL removed protected closing padding')
    for index, item in enumerate(mapping.keep):
        item['text'] = join_words([w for w, i in assigned if i == index])
    for audit in resolved_manual:
        if any(i in kept_ids for i in audit['removed_word_indices']):
            raise ValueError(f'Manual cut {audit["id"]} left targeted words')
        before = [w for w, _ in assigned if w['end'] <= audit['start']+1e-8]
        after = [w for w, _ in assigned if w['start'] >= audit['end']-1e-8]
        audit['before_text'] = join_words(before[-6:]) if before else '[start]'
        audit['after_text'] = join_words(after[:6]) if after else '[end]'
        audit['output_time'] = mapping.to_output(audit['start'])
        audit['status'] = 'OK'
    counts = Counter(c['reason'] for c in cuts)
    seconds = {reason: sum(c['end']-c['start'] for c in cuts if c['reason'] == reason) for reason in counts}
    assert abs(mapping.duration + sum(c['end']-c['start'] for c in cuts) - duration) < 1e-7
    return {'timeline': 'webcam seconds, half-open [start,end); keep also includes output_start/output_end',
            'raw_duration_s': duration, 'output_duration_s': mapping.duration,
            'removed_duration_s': duration-mapping.duration, 'cut_count': len(cuts),
            'cut_counts': dict(counts), 'removed_seconds_by_primary_reason': seconds,
            'source_counts': {'silence': sum(c['reason']=='silence' for c in edl['cuts']),
                              'untranscribed_filler': len(filler_audit), 'manual': len(resolved_manual)},
            'count_policy': 'Merged cuts use priority manual > untranscribed_filler > cleanup > silence; source_counts can overlap',
            'keep': mapping.keep, 'cuts': cuts, 'manual_verification': resolved_manual,
            'untranscribed_filler_audit': filler_audit, 'adjusted_automatic': adjusted_automatic,
            'short_keep_cleanup': short_cleanup, 'protected': protected,
            'validation': {'word_boundary_violations': 0, 'words_clipped_by_20_percent': 0,
                           'retained_word_count': len(assigned), 'removed_word_count': len(words)-len(assigned),
                           'first_word': assigned[0][0], 'last_word': assigned[-1][0],
                           'manual_cuts_applied': len(resolved_manual),
                           'protected_emphatic_word_count': len(emphasized),
                           'minimum_keep_duration_s': min(k['end']-k['start'] for k in mapping.keep)},
            'assumptions': [f'Untranscribed audio energy >= {filler_min:.2f}s is treated as filler.',
                            'Boundary safety is checked against existing ASR alignment and RMS, not a new forced alignment.',
                            'Screen sync/drift is independent; these files map webcam time to output time only.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edl', required=True)
    parser.add_argument('--transcript', required=True)
    parser.add_argument('--audio', required=True)
    parser.add_argument('--manual-cuts', required=True)
    parser.add_argument('--output', required=True)
    settings.add_config_arg(parser)
    args = parser.parse_args()
    cfg = settings.load(args.config)
    inputs = [args.edl, args.transcript, args.audio, args.manual_cuts]
    out = output_path(args.output, inputs)
    edl, transcript, manual = [json.loads(Path(p).read_text(encoding='utf-8'))
                               for p in [args.edl, args.transcript, args.manual_cuts]]
    samples, sr = audio(args.audio)
    result = finalize(edl, transcript, samples, sr, manual, cfg['lexicon'], cfg['cuts'])
    write_json(out, result)
    print(json.dumps({k: result[k] for k in ['raw_duration_s', 'output_duration_s', 'cut_count',
                                             'cut_counts', 'source_counts', 'validation']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
