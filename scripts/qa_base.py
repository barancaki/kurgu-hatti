"""Verify a rendered base and create a labeled contact sheet without network access."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import re
import subprocess

from scipy import signal

from common import audio, output_path, write_json
from render_base import offset_at
from render_common import json_measure, probe, run
import settings
from sync import residual_lag
from timemap import TimeMap, format_time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',required=True)
    parser.add_argument('--edl',required=True)
    parser.add_argument('--layout',required=True)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--qa-dir',required=True)
    settings.add_config_arg(parser)
    args = parser.parse_args()
    settings.load(args.config)
    qa = output_path(Path(args.qa_dir)/'base-qa.json',[args.video,args.edl,args.layout,args.plan]).parent
    edl,layout,plan = [json.loads(Path(p).read_text(encoding='utf-8')) for p in [args.edl,args.layout,args.plan]]
    sync = json.loads(Path(layout['sync']).read_text(encoding='utf-8'))
    mapping = TimeMap(edl['keep'],edl['raw_duration_s'])
    metadata = probe(args.video)
    video = next(s for s in metadata['streams'] if s['codec_type']=='video')
    audio_stream = next(s for s in metadata['streams'] if s['codec_type']=='audio')
    assert (video['width'],video['height'],video['r_frame_rate'],video['codec_name'])==(1920,1080,'30/1','h264')
    assert (audio_stream['sample_rate'],audio_stream['channels'],audio_stream['codec_name'])==('48000',2,'aac')
    difference = float(metadata['format']['duration'])-mapping.duration
    assert abs(difference)<=.1
    decode = subprocess.run(['ffmpeg','-v','error','-i',args.video,'-f','null','-'],capture_output=True)
    (qa/'decode-errors.log').write_bytes(decode.stderr)
    assert decode.returncode==0 and not decode.stderr.strip(), decode.stderr[-2000:]
    print('PASS full video/audio decode',flush=True)
    loudness = json_measure(run(['-i',args.video,'-vn','-af',settings.loudnorm(0)+':print_format=json',
                                 '-f','null','-'],qa/'base-loudness.log'))
    sync_results = []
    for i,web_time in enumerate(layout.get('qa_sync_webcam_times',[330,466,610])):
        out_time = mapping.to_output(web_time)
        keep = next(k for k in mapping.keep if k['start']<=web_time<k['end'])
        if web_time+2>keep['end']:
            raise ValueError('QA sync point crosses an EDL cut')
        screen_time = web_time-offset_at(web_time,sync)
        pair = []
        for name,source,t in [('base',args.video,out_time),('screen',layout['sources']['screen'],screen_time)]:
            target = qa/f'sync-{i+1}-{name}.wav'
            run(['-loglevel','error','-ss',f'{t:.9f}','-i',source,'-t','2','-vn','-ac','1','-ar','16000',target])
            samples,sr = audio(target)
            sos = signal.butter(3,[180,3200],btype='bandpass',fs=sr,output='sos')
            pair.append(signal.sosfiltfilt(sos,samples))
        lag,corr = residual_lag(pair[0],pair[1],16000,limit=.15)
        sync_results.append({'webcam_time':web_time,'output_time':out_time,'screen_time':screen_time,
                             'residual_ms':lag*1000,'correlation':corr,'passed':abs(lag)<=.05 and corr>.2})
    write_json(qa/'sync-proof.json',sync_results)
    assert all(r['passed'] for r in sync_results),sync_results
    entries = []
    for i,t in enumerate(plan['transitions'],1):
        for suffix,time in [('before',t['output_start']-1/30),('middle',(t['output_start']+t['output_end'])/2),
                            ('after',t['output_end']+1/30)]:
            entries.append({'id':f'mode{i}-{suffix}','time':time})
    for z in plan['zooms']:
        for suffix,time in [('before',z['output_start']-1/30),('hold',(z['output_start']+z['output_end'])/2),
                            ('after',z['output_end']+1/30)]:
            entries.append({'id':z['id']+'-'+suffix,'time':time})
    points = json.loads(Path(layout['cut_points']).read_text(encoding='utf-8'))['output_times']
    def is_talk(t):
        return any(m['mode']=='TALK' and m['output_start']+.5<t<m['output_end']-.5 for m in plan['modes'])
    selected = random.Random(20261016).sample([t for t in points if is_talk(t)],6)
    for i,t in enumerate(selected,1):
        for suffix,time in [('before',t-1/30),('after',t+1/30)]:
            entries.append({'id':f'talkcut{i}-{suffix}','time':time})
    for i,e in enumerate(entries):
        e['index']=i
        e['webcam_time']=mapping.to_webcam(e['time'])
        e['file']=str(qa/f'contact-{i:03}.jpg')
    def snapshot(item):
        label = f'{item["id"]}  out {format_time(item["time"]).replace(":","m")}s'
        run(['-loglevel','error','-ss',f'{item["time"]:.9f}','-i',args.video,'-frames:v','1',
             '-vf',f"scale=480:270,pad=480:300:0:0:black,drawtext=text='{label}':fontcolor=white:fontsize=17:x=8:y=277",
             '-threads','1',item['file']])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(snapshot,entries))
    rows = (len(entries)+3)//4
    run(['-loglevel','error','-framerate','1','-start_number','0','-i',qa/'contact-%03d.jpg',
         '-vf',f'tile=4x{rows}:nb_frames={len(entries)}:padding=2:margin=2',
         '-frames:v','1','-threads','1',qa/'base-contact.jpg'])
    write_json(qa/'contact-frames.json',{'random_seed':20261016,'entries':entries})
    for z in plan['zooms']:
        t = (z['output_start']+z['output_end'])/2
        run(['-loglevel','error','-ss',f'{t:.9f}','-i',args.video,'-frames:v','1','-threads','1',qa/f'zoom-{z["id"]}.jpg'])
    black_log = run(['-i',args.video,'-an','-vf','blackdetect=d=0.03:pix_th=0.1:pic_th=0.99',
                     '-f','null','-'],qa/'blackdetect.log')
    black = re.findall(r'black_start:([0-9.]+) black_end:([0-9.]+) black_duration:([0-9.]+)',black_log)
    results = {'metadata':metadata,'edl_duration_s':mapping.duration,'duration_difference_s':difference,
               'decode_errors':False,'integrated_lufs':float(loudness['input_i']),
               'true_peak_dbtp':float(loudness['input_tp']),'loudness_measurement':loudness,
               'sync_proof':sync_results,'black_intervals':black,'contact_sheet':str(qa/'base-contact.jpg'),
               'contact_frames':len(entries),'visual_review':'pending human/model contact-sheet inspection'}
    write_json(qa/'base-qa.json',results)
    assert abs(results['integrated_lufs']+14)<=.3, results['integrated_lufs']
    assert results['true_peak_dbtp']<=-1.,results['true_peak_dbtp']
    assert not black,black
    print(json.dumps({k:results[k] for k in ['edl_duration_s','duration_difference_s','integrated_lufs','true_peak_dbtp',
                                           'sync_proof','black_intervals','contact_frames']},ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    main()
