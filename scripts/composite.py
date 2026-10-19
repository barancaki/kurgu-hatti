"""Composite a base video using project-relative overlay/SFX paths.

JSON: {"overlays":[{"id":"intro","file":"overlays/intro.mov","start":0,"mode":"full|alpha"}],
"sfx":[{"file":"sfx/library/whoosh-soft.mp3","start":12.3,"gain_db":-18}],
"prepend":[{"file":"overlays/intro.mp4"}],"append":[{"file":"overlays/outro.mp4"}],
"music":{"file":null,"gain_db":-26,"duck":true}}
All overlay/SFX starts are relative to the base; prepend duration is added automatically.
Missing media is warned and skipped. Prepend/append preserve their own audio.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys

from common import output_path, write_json
import settings
from render_common import concat_file, encoder, json_measure, probe, run, video_options


def available(entries, kind, warnings):
    result = []
    for item in entries:
        filename = item.get('file')
        if not filename or not Path(filename).is_file():
            message = f'SKIP {kind}: missing file {filename}'
            warnings.append(message)
            print(message,file=sys.stderr)
            continue
        result.append(dict(item, metadata=probe(filename)))
    return result


def normalized_clip(source, target, enc, width, height, base=False, fade=False):
    metadata = probe(source)
    total = math.ceil(float(metadata['format']['duration'])*30-1e-7)/30
    has_audio = any(s['codec_type']=='audio' for s in metadata['streams'])
    args = ['-loglevel','error','-i',source]
    if not has_audio:
        args += ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
    audio_input = '0:a:0' if has_audio else '1:a:0'
    audio_filter = 'aresample=48000,aformat=channel_layouts=stereo,apad,'+f'atrim=duration={total:.9f},asetpts=PTS-STARTPTS'
    if fade:
        audio_filter += f',afade=t=out:st={max(0,total-.03):.9f}:d=0.03'
    args += ['-map','0:v:0','-map',audio_input,'-af',audio_filter,'-c:a','pcm_s24le']
    if base and any(s.get('width')==width and s.get('height')==height and s.get('r_frame_rate')=='30/1'
                    for s in metadata['streams'] if s['codec_type']=='video'):
        args += ['-c:v','copy']
    else:
        args += ['-vf',f'fps=30,scale={width}:{height}:force_original_aspect_ratio=decrease,'
                  f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration=1']
        args += video_options(enc)
    run(args+['-t',f'{total:.9f}',target],target.with_suffix('.log'))
    return total



def overlay_windows(overlays, prefix, total, window_s=30):
    frames = round(total*30)
    for first in range(0,frames,round(window_s*30)):
        count = min(round(window_s*30),frames-first)
        start,end = first/30,(first+count)/30
        active = []
        for item in overlays:
            when = float(item.get('start',0))+prefix
            duration = float(item['metadata']['format']['duration'])
            if not math.isfinite(when) or when<prefix or item.get('mode','alpha') not in ['alpha','full']:
                raise ValueError('Invalid overlay time/mode')
            if when<end and when+duration>start:
                active.append(dict(item,seek=max(0,start-when),local_start=max(0,when-start)))
        yield first,count,active


def windowed_video(joined, overlays, prefix, total, work, enc, width, height, window_s=30):
    files,durations = [],[]
    source = Path(joined).stat()
    for first,count,active in overlay_windows(overlays,prefix,total,window_s):
        signature = json.dumps([first,count,active,enc,width,height,source.st_size,source.st_mtime_ns],sort_keys=True).encode()
        dest = work/f'window-{first:06}-{hashlib.sha256(signature).hexdigest()[:12]}.mp4'
        valid = False
        if dest.exists():
            try:
                stream = next(s for s in probe(dest)['streams'] if s['codec_type']=='video')
                valid = int(stream.get('nb_frames',0))==count and stream['width']==width
            except (ValueError,RuntimeError,StopIteration):
                pass
        if not valid:
            args = ['-loglevel','error','-filter_complex_threads','2','-ss',f'{first/30:.9f}',
                    '-threads','2','-i',joined]
            graph = ['[0:v]setpts=PTS-STARTPTS[base]']
            label = 'base'
            for n,item in enumerate(active,1):
                args += ['-ss',f'{item["seek"]:.9f}','-threads','2','-i',item['file']]
                filt = f'[{n}:v]fps=30,scale={width}:{height},format=rgba'
                if item.get('mode','alpha')=='full':
                    filt += ',lut=a=255'
                graph.append(filt+f',setpts=PTS-STARTPTS+{item["local_start"]:.9f}/TB[o{n}]')
                graph.append(f'[{label}][o{n}]overlay=0:0:eof_action=pass:repeatlast=0[v{n}]')
                label = f'v{n}'
            script = dest.with_suffix('.ffgraph')
            script.write_text(';'.join(graph),encoding='utf-8')
            run(args+['-filter_complex_script',script,'-map',f'[{label}]','-an','-frames:v',count]+
                video_options(enc)+[dest],dest.with_suffix('.log'))
        files.append(dest);durations.append(count/30)
        print(f'WINDOW {len(files)} frames={first}:{first+count} overlays={len(active)} cached={valid}',flush=True)
    listing = work/'video-windows.txt'
    concat_file(files,listing,durations)
    target = work/'composited-video.mp4'
    run(['-loglevel','error','-f','concat','-safe','0','-i',listing,'-an','-c:v','copy',target],work/'video-windows.log')
    return target

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,help='Base MP4')
    parser.add_argument('--overlays',required=True,help='Overlay specification JSON')
    parser.add_argument('--output',required=True)
    parser.add_argument('--work-dir',required=True)
    parser.add_argument('--preview',action='store_true',help='960x540, fast video preset')
    parser.add_argument('--sfx-library',default='sfx/library',help='Folder listed in the report as available SFX')
    settings.add_config_arg(parser)
    args = parser.parse_args()
    cfg = settings.load(args.config)
    config =json.loads(Path(args.overlays).read_text(encoding='utf-8'))
    warnings = []
    overlays = available(config.get('overlays',[]),'overlay',warnings)
    sfx = available(config.get('sfx',[]),'SFX',warnings)
    prepend = available(config.get('prepend',[]),'prepend',warnings)
    append = available(config.get('append',[]),'append',warnings)
    music_config = config.get('music') or {}
    music = available([music_config],'music',warnings) if music_config.get('file') else []
    inputs = [args.input,args.overlays]+[i['file'] for group in [overlays,sfx,prepend,append,music] for i in group]
    out = output_path(args.output,inputs)
    work = output_path(Path(args.work_dir)/'composite-result.json',inputs).parent
    metadata = probe(args.input)
    width,height = (960,540) if args.preview else (1920,1080)
    if not any([overlays,sfx,prepend,append,music]) and not args.preview:
        shutil.copyfile(args.input,out)
        write_json(work/'composite-result.json',{'output':str(out),'mode':'no-overlays copy','warnings':warnings,
                                                'prepend_shift_s':0,'metadata':probe(out)})
        print(f'COMPLETE {out} (no overlays, unchanged audio/video)')
        return
    enc = encoder(args.preview)
    clips, clip_durations, prefix = [], [], 0.
    for index,item in enumerate(prepend):
        dest = work/f'prepend-{index:03}.mkv'
        clip_duration = normalized_clip(item['file'],dest,enc,width,height)
        prefix += clip_duration
        clips.append(dest)
        clip_durations.append(clip_duration)
    base_path = work/'base.mkv'
    base_duration = normalized_clip(args.input,base_path,enc,width,height,base=True,fade=bool(append))
    clips.append(base_path)
    clip_durations.append(base_duration)
    suffix = 0.
    for index,item in enumerate(append):
        dest = work/f'append-{index:03}.mkv'
        clip_duration = normalized_clip(item['file'],dest,enc,width,height)
        suffix += clip_duration
        clips.append(dest)
        clip_durations.append(clip_duration)
    total = prefix+base_duration+suffix
    listing = work/'concat.txt'
    concat_file(clips,listing,clip_durations)
    joined = work/'joined.mkv'
    run(['-loglevel','error','-f','concat','-safe','0','-i',listing,'-c','copy',joined],work/'concat.log')
    video = windowed_video(joined,overlays,prefix,total,work,enc,width,height)
    args_ff = ['-loglevel','error','-filter_complex_threads','2','-i',joined]
    graph, input_index = [], 1
    audio_labels = ['0:a']
    for number,item in enumerate(sfx):
        start = float(item.get('start',0))+prefix
        gain_db = float(item.get('gain_db',cfg['audio']['sfx_gain_db']))
        if not math.isfinite(start) or start<prefix or not math.isfinite(gain_db):
            raise ValueError('Invalid SFX time/gain')
        args_ff += ['-i',item['file']]
        graph.append(f'[{input_index}:a]aresample=48000,aformat=channel_layouts=stereo,'
                     f'volume={10**(gain_db/20):.12f},adelay={round(start*48000)}S:all=1,'
                     f'apad,atrim=duration={total:.9f}[sfx{number}]')
        audio_labels.append(f'sfx{number}')
        input_index += 1
    if music:
        item = music[0]
        gain_db = float(item.get('gain_db',cfg['audio']['music_gain_db']))
        if not math.isfinite(gain_db):
            raise ValueError('Invalid music gain')
        args_ff += ['-stream_loop','-1','-i',item['file']]
        graph.append(f'[{input_index}:a]aresample=48000,aformat=channel_layouts=stereo,'
                     f'volume={10**(gain_db/20):.12f},atrim=duration={total:.9f}[music]')
        input_index += 1
        if item.get('duck',True):
            args_ff += ['-i',args.input]
            graph.append(f'[{input_index}:a]aresample=48000,aformat=channel_layouts=stereo,'
                         f'adelay={round(prefix*48000)}S:all=1,apad,atrim=duration={total:.9f}[voicekey]')
            graph.append('[music][voicekey]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[duckmusic]')
            audio_labels.append('duckmusic')
        else:
            audio_labels.append('music')
    if len(audio_labels)>1:
        graph.append(''.join(f'[{a}]' for a in audio_labels)+
                     f'amix=inputs={len(audio_labels)}:duration=first:normalize=0,'
                     'aresample=192000,alimiter=limit=0.88:level=false:latency=1,aresample=48000[audio]')
        audio_label = '[audio]'
    else:
        audio_label = '0:a:0'
    if graph:
        script = work/'composite-filter.txt'
        script.write_text(';'.join(graph),encoding='utf-8')
        args_ff += ['-filter_complex_script',script]
    mixed = work/'mixed.wav'
    args_ff += ['-map',audio_label,'-vn','-c:a','pcm_s24le','-ar','48000','-ac','2','-t',f'{total:.9f}',mixed]
    run(args_ff,work/'audio-mix.log')
    loud = settings.loudnorm(.7, cfg)
    measure = json_measure(run(['-i',mixed,'-vn','-af',loud+':print_format=json','-f','null','-'],work/'loudnorm-pass1.log'))
    second = (loud+f':measured_I={measure["input_i"]}:measured_TP={measure["input_tp"]}'
              f':measured_LRA={measure["input_lra"]}:measured_thresh={measure["input_thresh"]}'
              f':offset={measure["target_offset"]}:linear=true:print_format=json')
    mastered = work/'mastered.wav'
    mastering = json_measure(run(['-i',mixed,'-af',second,'-ar','48000','-c:a','pcm_s24le',mastered],work/'loudnorm-pass2.log'))
    run(['-loglevel','error','-i',video,'-i',mastered,'-map','0:v:0','-map','1:a:0',
         '-c:v','copy','-c:a','aac','-ar','48000','-ac','2','-b:a','192k','-t',f'{total:.9f}',
         '-movflags','+faststart',out],work/'composite.log')
    library = Path(args.sfx_library)
    write_json(work/'composite-result.json',{'output':str(out),'prepend_shift_s':prefix,
                                            'base_duration_s':base_duration,'append_duration_s':suffix,
                                            'output_duration_s':total,'warnings':warnings,
                                            'available_sfx':sorted(p.name for p in library.glob('*.mp3')),
                                            'mastering':{'input':measure,'output':mastering},'metadata':probe(out)})
    print(f'COMPLETE {out}; overlay/SFX shift={prefix:.3f}s; duration={total:.3f}s')


if __name__=='__main__':
    main()
