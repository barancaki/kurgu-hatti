"""Offline final-video QA: timing, alpha, chapter transitions and audio levels."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

import numpy as np

from common import output_path, write_json
import settings
from render_common import json_measure, probe, run
from timemap import format_time


def rgb(source, t, rgba=False):
    # Two milliseconds stays on the intended frame across MP4 and millisecond MKV timestamps.
    result = subprocess.run(['ffmpeg','-v','error','-ss',f'{max(0,t-.002):.9f}','-threads','2','-i',str(source),
                             '-frames:v','1','-threads','1','-f','rawvideo','-pix_fmt','rgba' if rgba else 'rgb24','-'],
                            capture_output=True,check=True)
    return np.frombuffer(result.stdout,dtype=np.uint8).reshape(1080,1920,4 if rgba else 3)


def momentary(source, t):
    log = run(['-ss',f'{t:.9f}','-i',source,'-t','0.4','-vn','-af','ebur128=peak=true','-f','null','-'])
    values = re.findall(r'\bt:\s*[\d.]+\s+TARGET:.*?\bM:\s*(-?[\d.]+)',log)
    if not values:
        raise ValueError('No EBU momentary loudness')
    return float(values[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',required=True)
    parser.add_argument('--base',required=True)
    parser.add_argument('--overlays',required=True)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--qa-dir',required=True)
    parser.add_argument('--chapter-prefix',default='ch',help='Overlay ids like ch1, ch2 are chapter bumpers')
    parser.add_argument('--sfx-check',nargs='*',default=None,metavar='ID',
                        help='SFX ids to compare against speech loudness (default: up to 3 spread across the video)')
    settings.add_config_arg(parser)
    args = parser.parse_args()
    cfg = settings.load(args.config)
    qa = output_path(Path(args.qa_dir)/'final-qa.json',[args.video,args.base,args.overlays,args.plan,args.manifest]).parent
    config,plan,manifest = [json.loads(Path(p).read_text(encoding='utf-8')) for p in [args.overlays,args.plan,args.manifest]]
    prefix = manifest['prepend_shift_s']
    base_duration = float(probe(args.base)['format']['duration'])
    total = prefix+base_duration+manifest['append_duration_s']
    metadata = probe(args.video)
    v = next(s for s in metadata['streams'] if s['codec_type']=='video')
    a = next(s for s in metadata['streams'] if s['codec_type']=='audio')
    assert (v['width'],v['height'],v['r_frame_rate'],v['codec_name'])==(1920,1080,'30/1','h264')
    assert (a['sample_rate'],a['channels'],a['codec_name'])==('48000',2,'aac')
    assert abs(float(metadata['format']['duration'])-total)<=.1
    decode = subprocess.run(['ffmpeg','-v','error','-i',args.video,'-f','null','-'],capture_output=True)
    (qa/'decode-errors.log').write_bytes(decode.stderr)
    assert decode.returncode==0 and not decode.stderr.strip(),decode.stderr[-2000:]
    print('PASS full decode and format/duration',flush=True)
    loudness = json_measure(run(['-i',args.video,'-vn','-af',settings.loudnorm(0,cfg)+':print_format=json',
                                 '-f','null','-'],qa/'final-loudness.log'))
    assert settings.loudness_ok(loudness,cfg),loudness
    print('PASS loudness',loudness['input_i'],loudness['input_tp'],flush=True)
    overlays = []
    for item in config['overlays']:
        duration = float(probe(item['file'])['format']['duration'])
        overlays.append(dict(item,actual_duration=duration,final_start=item['start']+prefix))
    entries = [{'id':'intro-middle','time':prefix/2}]
    for item in overlays:
        entries.append({'id':item['id'],'time':item['final_start']+item['actual_duration']/2})
    entries.append({'id':'outro-middle','time':prefix+base_duration+manifest['append_duration_s']/2})
    for n,item in enumerate(entries):
        item['file']=str(qa/f'overlay-{n:03}.jpg')
        item['time']=math.floor(item['time']*30+1e-6)/30
    def snapshot(item):
        label=f'{item["id"]}  final {format_time(item["time"]).replace(":","m")}s'
        run(['-loglevel','error','-ss',max(0,item['time']-.002),'-threads','2','-i',args.video,'-frames:v','1',
             '-vf',f"scale=480:270,pad=480:300:0:0:black,drawtext=text='{label}':fontcolor=white:fontsize=17:x=6:y=277",
             '-threads','1',item['file']])
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(snapshot,entries))
    rows=(len(entries)+3)//4
    run(['-loglevel','error','-framerate','1','-i',qa/'overlay-%03d.jpg','-vf',
         f'tile=4x{rows}:nb_frames={len(entries)}:padding=2:margin=2','-frames:v','1','-threads','1',qa/'overlays-contact.jpg'])
    chapters=[]
    for item in overlays:
        if re.fullmatch(re.escape(args.chapter_prefix)+r'\d+',item['id']):
            start,end=item['final_start'],item['final_start']+item['actual_duration']
            for suffix,t in [('before',start-1/30),('middle',(start+end)/2),('after',end+1/30)]:
                chapters.append({'id':item['id']+'-'+suffix,'time':t,'file':str(qa/f'chapter-{len(chapters):03}.jpg')})
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(snapshot,chapters))
    if chapters:
        run(['-loglevel','error','-framerate','1','-i',qa/'chapter-%03d.jpg','-vf',
             f'tile=3x{len(chapters)//3}:nb_frames={len(chapters)}:padding=2:margin=2',
             '-frames:v','1','-threads','1',qa/'chapters-contact.jpg'])
    coverage=[]
    for t in plan['transitions']:
        covering=[o['id'] for o in overlays if o['final_start']<=prefix+t['output_start']
                  and o['final_start']+o['actual_duration']>=prefix+t['output_end']]
        coverage.append({'mode_from':t['from'],'mode_to':t['to'],'final_start':prefix+t['output_start'],
                         'final_end':prefix+t['output_end'],'covering_overlays':covering})
    alpha_proof=[]
    yy,xx=np.mgrid[:1080,:1920]
    circle=(xx-1730)**2+(yy-890)**2<146**2
    for item in overlays:
        entry=next(e for e in entries if e['id']==item['id'])
        t=entry['time']
        foreground=rgb(item['file'],math.floor((t-item['final_start'])*30+1e-6)/30,True)
        final=rgb(args.video,t)
        base=rgb(args.base,t-prefix)
        alpha=foreground[:,:,3]/255.
        transparent=alpha<.005
        outside_error=float(np.mean(np.abs(final.astype('float32')-base.astype('float32'))[transparent])) if np.any(transparent) else None
        indices=np.argwhere(alpha>.05)
        bbox=[int(indices[:,1].min()),int(indices[:,0].min()),int(indices[:,1].max()),int(indices[:,0].max())] if len(indices) else None
        mode=next(m['mode'] for m in plan['modes'] if m['output_start']<=t-prefix<m['output_end'])
        alpha_proof.append({'id':item['id'],'final_midpoint':t,'mode':mode,'alpha_bbox':bbox,
                            'transparent_pixels_mean_error_8bit':outside_error,
                            'pip_opaque_coverage_pct':float(np.mean(alpha[circle]>.5)*100) if mode=='SCREEN' else None})
        print('ALPHA',item['id'],mode,bbox,outside_error,flush=True)
    write_json(qa/'visual-measurements.json',{'overlay_frames':entries,'chapter_frames':chapters,
                                               'mode_coverage':coverage,'alpha_proof':alpha_proof})
    first=overlays[0]
    shift_proof=[]
    for suffix,base_time in [('before',first['start']-.1),('visible',first['start']+.8)]:
        base_time=math.floor(base_time*30+1e-6)/30
        final=rgb(args.video,base_time+prefix)
        base=rgb(args.base,base_time)
        region=np.s_[150:900,1180:1870]
        shift_proof.append({'phase':suffix,'base_time':base_time,'final_time':base_time+prefix,
                            'right_card_region_mean_difference':float(np.abs(final.astype('float32')-base.astype('float32'))[region].mean())})
    sfx_results=[]
    sfx=[x for x in config.get('sfx',[]) if Path(x.get('file') or '').is_file()]
    if args.sfx_check is not None:
        checked=[x for x in sfx if x.get('id') in set(args.sfx_check)]
    else:
        checked=[sfx[round(i*(len(sfx)-1)/2)] for i in range(3)] if len(sfx)>=3 else sfx
    for item in checked:
        before=momentary(args.base,item['start'])
        after=momentary(args.video,item['start']+prefix)
        sfx_results.append({'id':item.get('id',Path(item['file']).stem),'base_time':item['start'],'final_time':item['start']+prefix,
                            'speech_momentary_lufs':before,'final_momentary_lufs':after,'difference_db':after-before,
                            'passed':abs(after-before)<=3})
    # Boundary sample jumps are evidence of clicks; quiet source tails are not inserted gaps.
    seams=[]
    for t in [prefix,prefix+base_duration]:
        raw=subprocess.check_output(['ffmpeg','-v','error','-ss',str(t-.02),'-i',args.video,'-t','0.04',
                                     '-vn','-ar','48000','-ac','1','-f','f32le','-'])
        samples=np.frombuffer(raw,dtype='<f4')
        seams.append({'time':t,'max_adjacent_sample_step_40ms':float(np.max(abs(np.diff(samples)))),
                      'boundary_sample_step':float(abs(samples[960]-samples[959]))})
    result={'metadata':metadata,'expected_duration_s':total,'duration_difference_s':float(metadata['format']['duration'])-total,
            'decode_errors':False,'integrated_lufs':float(loudness['input_i']),'true_peak_dbtp':float(loudness['input_tp']),
            'config_sha256':hashlib.sha256(Path(args.overlays).read_bytes()).hexdigest(),
            'prepend_shift_s':prefix,'overlay_frames':entries,'chapter_frames':chapters,'mode_coverage':coverage,
            'alpha_proof':alpha_proof,'first_overlay_shift_proof':shift_proof,'sfx_momentary':sfx_results,
            'audio_seams':seams,'visual_review':'pending contact-sheet inspection'}
    write_json(qa/'final-qa.json',result)
    assert len(entries)==len(overlays)+2 and len(chapters)%3==0
    assert all(x['passed'] for x in sfx_results),sfx_results
    assert shift_proof[1]['right_card_region_mean_difference']>shift_proof[0]['right_card_region_mean_difference']+3,shift_proof
    print(json.dumps({k:result[k] for k in ['expected_duration_s','integrated_lufs','true_peak_dbtp','mode_coverage','sfx_momentary','audio_seams']},ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    main()
