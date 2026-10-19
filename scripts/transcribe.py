"""Offline word-level transcription (faster-whisper) with conservative SRT wrapping."""
import argparse
import gc
import os
from pathlib import Path
import sys
import textwrap
import time

from common import duration, join_words, output_path, timestamp, write_json
import settings


def subtitles(segments):
    cues = []
    current = []

    def emit():
        if not current:
            return
        text = join_words(current)
        lines = textwrap.wrap(text, width=42, break_long_words=True, break_on_hyphens=False)
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines)
        start = current[0]['start']
        end = max(start + .001, current[-1]['end'])
        if cues:
            start = max(start, cues[-1][1])
            end = max(end, start + .001)
        cues.append((start, end, '\n'.join(lines)))
        current.clear()

    for segment in segments:
        for word in segment['words']:
            pieces = textwrap.wrap(word['word'].strip(), width=42, break_long_words=True, break_on_hyphens=False)
            for piece in pieces:
                item = dict(word, word=piece)
                proposed = join_words(current + [item])
                lines = textwrap.wrap(proposed, width=42, break_long_words=True, break_on_hyphens=False)
                if current and (len(lines) > 2 or item['end'] - current[0]['start'] > 6
                                or item['start'] - current[-1]['end'] > .7):
                    emit()
                current.append(item)
            if word['word'].rstrip().endswith(('.', '?', '!')) and current[-1]['end'] - current[0]['start'] >= 1:
                emit()
    emit()
    return '\n\n'.join(f'{i}\n{timestamp(s)} --> {timestamp(e)}\n{t}' for i, (s, e, t) in enumerate(cues, 1)) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', required=True, help='Input audio, on the webcam timeline')
    parser.add_argument('--model-root', required=True, help='Existing Hugging Face cache; never downloads')
    parser.add_argument('--model', help='Cached faster-whisper model name or local directory (default: transcribe.model)')
    parser.add_argument('--output', required=True, help='Output transcript JSON')
    parser.add_argument('--srt', required=True, help='Output SRT (42 columns, 2 lines)')
    parser.add_argument('--source-video', help='Optional source video, for exact raw video duration')
    parser.add_argument('--language', help='Whisper language code (default: language in settings)')
    parser.add_argument('--initial-prompt', help='Whisper prompt (default: lexicon.initial_prompt)')
    settings.add_config_arg(parser)
    args = parser.parse_args()
    cfg = settings.load(args.config)
    args.model = args.model or cfg['transcribe']['model']
    args.language = args.language or cfg['language']
    args.initial_prompt = args.initial_prompt or cfg['lexicon']['initial_prompt']
    inputs = [args.audio, args.source_video]
    out = output_path(args.output, inputs)
    srt = output_path(args.srt, inputs)
    if out == srt:
        parser.error('--output and --srt must differ')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    dll_handles = []
    if os.name == 'nt':
        folders = list((Path(sys.prefix) / 'Lib/site-packages/nvidia').glob('*/bin'))
        for folder in folders:
            dll_handles.append(os.add_dll_directory(str(folder.resolve())))
        # CTranslate2 also uses LoadLibrary, which requires PATH for delayed CUDA loads.
        os.environ['PATH'] = os.pathsep.join(str(p.resolve()) for p in folders) + os.pathsep + os.environ.get('PATH', '')
    from faster_whisper import WhisperModel
    import soundfile as sf
    audio_seconds = sf.info(args.audio).duration
    raw_seconds = duration(args.source_video) if args.source_video else audio_seconds
    errors = []
    started = time.monotonic()
    for device, compute in [('cuda', 'float16'), ('cuda', 'int8_float16'), ('cpu', 'int8')]:
        model = None
        try:
            print(f'Loading {args.model}: {device}/{compute}', flush=True)
            model = WhisperModel(args.model, device=device, compute_type=compute,
                                 download_root=args.model_root, local_files_only=True,
                                 cpu_threads=cfg['transcribe']['cpu_threads'])
            stream, info = model.transcribe(args.audio, language=args.language, word_timestamps=True,
                                            vad_filter=False, initial_prompt=args.initial_prompt,
                                            beam_size=cfg['transcribe']['beam_size'], condition_on_previous_text=True)
            segments = []
            for seg in stream:
                words = [{'start': float(w.start), 'end': float(w.end), 'word': w.word,
                          'probability': float(w.probability)} for w in seg.words or []]
                assert all(0 <= w['start'] <= w['end'] <= audio_seconds + .1 for w in words)
                segments.append({'id': seg.id, 'start': seg.start, 'end': seg.end, 'text': seg.text,
                                 'avg_logprob': seg.avg_logprob, 'no_speech_prob': seg.no_speech_prob,
                                 'words': words})
                print(f'{seg.end:7.2f}/{audio_seconds:.2f} {seg.text}', flush=True)
            data = {'audio': str(Path(args.audio).resolve()), 'source_video': args.source_video,
                    'duration': raw_seconds, 'audio_duration': audio_seconds,
                    'language': info.language, 'language_probability': info.language_probability,
                    'model': args.model, 'device': device, 'compute_type': compute,
                    'word_timestamps': True, 'vad_filter': False, 'condition_on_previous_text': True, 'initial_prompt': args.initial_prompt,
                    'alignment_warnings': [{'start': w['start'], 'end': w['end'], 'word': w['word'], 'reason': 'zero_duration'} for seg in segments for w in seg['words'] if w['end'] <= w['start']],
                    'fallback_errors': errors, 'elapsed_s': time.monotonic() - started,
                    'segments': segments}
            if not any(s['words'] for s in segments):
                raise ValueError('No word timestamps were produced')
            caption_text = subtitles(segments)
            write_json(out, data)
            temp = srt.with_name(srt.name + '.tmp')
            temp.write_text(caption_text, encoding='utf-8')
            os.replace(temp, srt)
            print(f'Wrote {out} and {srt}', flush=True)
            return
        except (RuntimeError, OSError, ValueError) as error:
            errors.append(f'{device}/{compute}: {error}')
            print(errors[-1], file=sys.stderr, flush=True)
        finally:
            del model
            gc.collect()
    raise RuntimeError('All offline backends failed: ' + '; '.join(errors))


if __name__ == '__main__':
    main()
