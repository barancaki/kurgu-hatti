"""Map master/webcam seconds to edited seconds and export the edited transcript.

Intervals are half-open. Deleted webcam time maps to the next keep's output start.
At an output jump, the inverse selects the following source keep (right-continuous).
After the final keep, deleted time maps to output duration.
"""
import argparse
from bisect import bisect_right
import json
import math
from pathlib import Path
import re

from common import join_words, output_path, write_json


class TimeMap:
    def __init__(self, keep, raw_duration):
        self.raw_duration = float(raw_duration)
        if not math.isfinite(self.raw_duration) or self.raw_duration <= 0 or not keep:
            raise ValueError('A positive raw duration and at least one keep are required')
        self.keep = []
        cursor = 0.
        last = 0.
        for item in keep:
            start, end = float(item['start']), float(item['end'])
            if not (math.isfinite(start) and math.isfinite(end) and last <= start < end <= self.raw_duration):
                raise ValueError('Keep intervals must be finite, sorted, disjoint and inside raw duration')
            self.keep.append(dict(item, output_start=cursor, output_end=cursor + end - start))
            cursor += end - start
            last = end
        self.duration = cursor
        self.ends = [k['end'] for k in self.keep]
        self.output_ends = [k['output_end'] for k in self.keep]

    def to_output(self, webcam_time):
        t = float(webcam_time)
        if not math.isfinite(t) or not 0 <= t <= self.raw_duration:
            raise ValueError('Webcam time outside raw duration')
        i = bisect_right(self.ends, t)
        if i == len(self.keep):
            return self.duration
        item = self.keep[i]
        return item['output_start'] + max(0., t - item['start'])

    def to_webcam(self, output_time):
        t = float(output_time)
        if not math.isfinite(t) or not 0 <= t <= self.duration:
            raise ValueError('Output time outside edited duration')
        i = bisect_right(self.output_ends, t)
        if i == len(self.keep):
            return self.keep[-1]['end']
        item = self.keep[i]
        return item['start'] + t - item['output_start']


def load_corrections(path):
    """Read optional ASR spelling rules: [{"pattern", "replacement", "start"?, "end"?}]."""
    if not path:
        return []
    rules = json.loads(Path(path).read_text(encoding='utf-8'))
    rules = rules.get('rules', []) if isinstance(rules, dict) else rules
    for rule in rules:
        re.compile(rule['pattern'])
        rule.setdefault('replacement', '')
    return rules


def correct_text(text, webcam_start, rules=()):
    """Apply only the requested contextual ASR spelling corrections (regex, case-insensitive).

    A rule with start/end applies only when the group starts in [start, end) webcam seconds.
    """
    for rule in rules:
        if 'start' in rule and not rule['start'] <= webcam_start < rule.get('end', math.inf):
            continue
        text = re.sub(rule['pattern'], rule['replacement'], text, flags=re.I)
    return text


def format_time(seconds):
    centiseconds = round(seconds * 100)
    minutes, remainder = divmod(centiseconds, 6000)
    return f'{minutes:02}:{remainder // 100:02}.{remainder % 100:02}'


def source_words(transcript):
    return [dict(w, source_word_index=i, segment_index=segment_index)
            for i, (segment_index, w) in enumerate(
                ( (si, word) for si, segment in enumerate(transcript['segments'])
                  for word in segment['words']))]


def word_membership(words, keep):
    """Return complete-word assignments, failing on any partial word."""
    assigned = []
    for word in words:
        start, end = word['start'], word['end']
        overlaps = [(i, k) for i, k in enumerate(keep)
                    if k['start'] < end - 1e-9 and k['end'] > start + 1e-9]
        if not overlaps:
            continue
        if len(overlaps) != 1 or start < overlaps[0][1]['start'] - 1e-8 or end > overlaps[0][1]['end'] + 1e-8:
            raise ValueError(f'Partial source word at {start}: {word["word"]}')
        assigned.append((word, overlaps[0][0]))
    return assigned


def export(edl, transcript, folder, input_paths=(), corrections=()):
    mapping = TimeMap(edl['keep'], edl['raw_duration_s'])
    if abs(transcript['duration'] - mapping.raw_duration) > .001:
        raise ValueError('Transcript and EDL durations differ')
    names = ['out_words.json', 'out_transcript.txt', 'cut_points.json']
    paths = [output_path(Path(folder) / n, input_paths) for n in names]
    words = source_words(transcript)
    assigned = word_membership(words, mapping.keep)
    output_words = []
    rows = []
    group = []
    key = None

    def emit():
        if group:
            rows.append(f'{format_time(group[0]["start"])}–{format_time(group[-1]["end"])} | '
                        + correct_text(join_words(group), group[0]['webcam_start'], corrections))
            group.clear()

    for word, index in assigned:
        item = mapping.keep[index]
        result = {'start': item['output_start'] + word['start'] - item['start'],
                  'end': item['output_start'] + word['end'] - item['start'],
                  'webcam_start': word['start'], 'webcam_end': word['end'],
                  'word': word['word'].strip(), 'probability': word.get('probability'),
                  'source_word_index': word['source_word_index'], 'segment_index': word['segment_index'],
                  'keep_index': index}
        next_key = (index, word['segment_index'])
        if key != next_key:
            emit()
        key = next_key
        output_words.append(result)
        group.append(result)
    emit()
    points = []
    for i, (before, after) in enumerate(zip(mapping.keep, mapping.keep[1:])):
        if after['start'] > before['end'] + 1e-9:
            sources = [c for c in edl['cuts']
                       if c['start'] < after['start'] and c['end'] > before['end']]
            points.append({'output_time': after['output_start'], 'webcam_before': before['end'],
                           'webcam_after': after['start'], 'removed_s': after['start'] - before['end'],
                           'reasons': sorted({r for c in sources for r in c.get('reasons', [c['reason']])}),
                           'manual_ids': sorted({n for c in sources for n in c.get('manual_ids', [])})})
    write_json(paths[0], {'timeline': 'start/end = output seconds; webcam_start/end = original master seconds',
                         'raw_duration_s': mapping.raw_duration, 'output_duration_s': mapping.duration,
                         'word_count': len(output_words), 'removed_word_count': len(words) - len(output_words),
                         'text_policy': 'Original ASR words and probabilities; spelling corrections only in out_transcript.txt',
                         'words': output_words})
    paths[1].write_text('\n'.join(rows) + '\n', encoding='utf-8')
    write_json(paths[2], {'timeline': 'output seconds; internal source jumps only, excluding head/tail trims',
                         'output_duration_s': mapping.duration, 'count': len(points),
                         'output_times': [p['output_time'] for p in points], 'cut_points': points,
                         'head_trim_s': mapping.keep[0]['start'],
                         'tail_trim_s': mapping.raw_duration - mapping.keep[-1]['end']})
    return {'output_duration_s': mapping.duration, 'words': len(output_words), 'segments': len(rows),
            'internal_cut_points': len(points)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edl', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--to-output', nargs='+', type=float, metavar='WEBCAM_SECONDS')
    group.add_argument('--to-webcam', nargs='+', type=float, metavar='OUTPUT_SECONDS')
    group.add_argument('--export-dir', help='Write out_words.json, out_transcript.txt and cut_points.json')
    parser.add_argument('--transcript', help='Original transcript JSON; required with --export-dir')
    parser.add_argument('--corrections', help='Optional ASR spelling rules JSON (see examples/asr_corrections.json)')
    args = parser.parse_args()
    edl = json.loads(Path(args.edl).read_text(encoding='utf-8'))
    mapping = TimeMap(edl['keep'], edl['raw_duration_s'])
    if args.export_dir:
        if not args.transcript:
            parser.error('--export-dir requires --transcript')
        transcript = json.loads(Path(args.transcript).read_text(encoding='utf-8'))
        result = export(edl, transcript, args.export_dir, [args.edl, args.transcript],
                        load_corrections(args.corrections))
    elif args.to_output is not None:
        result = [{'webcam_time': t, 'output_time': mapping.to_output(t)} for t in args.to_output]
    else:
        result = [{'output_time': t, 'webcam_time': mapping.to_webcam(t)} for t in args.to_webcam]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
