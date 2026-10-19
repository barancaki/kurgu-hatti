"""Check music-only loop joins and the approved-video music mix without network access."""
import argparse
import json
import math
from pathlib import Path
import subprocess

import numpy as np
import soundfile as sf

from common import output_path, write_json
from mix_music import checksum, loudness, short_term, video_hash
from render_common import probe, run


def power(x):
    return float(np.mean(x.astype('float64')**2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,help='Approved v1')
    parser.add_argument('--video',required=True,help='Music v2')
    parser.add_argument('--mix-report',required=True)
    parser.add_argument('--qa-dir',required=True)
    args=parser.parse_args()
    report=json.loads(Path(args.mix_report).read_text(encoding='utf-8'))
    bed_report=json.loads(Path(report['bed_report']).read_text(encoding='utf-8'))
    bed=Path(bed_report['output'])
    qa=output_path(Path(args.qa_dir)/'music-qa.json',[args.input,args.video,args.mix_report,bed]).parent
    before,after=probe(args.input),probe(args.video)
    duration=float(before['format']['duration'])
    stream=next(s for s in after['streams'] if s['codec_type']=='video')
    audio=next(s for s in after['streams'] if s['codec_type']=='audio')
    assert (stream['width'],stream['height'],stream['r_frame_rate'])==(1920,1080,'30/1')
    assert (audio['sample_rate'],audio['channels'],audio['codec_name'])==('48000',2,'aac')
    assert abs(float(after['format']['duration'])-duration)<=.05
    copied_hash=video_hash(args.video)
    original_hash=video_hash(args.input)
    assert copied_hash==original_hash
    decode=subprocess.run(['ffmpeg','-v','error','-i',args.video,'-f','null','-'],capture_output=True)
    (qa/'decode-errors.log').write_bytes(decode.stderr)
    assert decode.returncode==0 and not decode.stderr.strip(),decode.stderr[-2000:]
    stats=loudness(args.video)
    assert abs(float(stats['input_i'])+14)<=.5 and float(stats['input_tp'])<=-1,stats
    print('PASS video packet copy, full decode, duration, LUFS/TP',flush=True)
    starts=[60,300,540] if duration>554 else [report['intro_s']+(duration-report['intro_s']-report['outro_s']-10)*f for f in [.1,.5,.85]]
    speech=[]
    for t in starts:
        v1=short_term(args.input,t)
        v2=short_term(args.video,t)
        speech.append({'start':t,'end':t+10,'v1_mean_short_term_lufs':v1,
                       'v2_mean_short_term_lufs':v2,'difference_db':v2-v1,'passed':abs(v2-v1)<=1})
    music_bus=report['music_bus']
    intro=float(loudness(music_bus,0,report['intro_s'])['input_i'])
    outro=float(loudness(music_bus,duration-report['outro_s'],report['outro_s'])['input_i'])
    loops=[]
    with sf.SoundFile(bed) as source:
        sr=source.samplerate
        for number,item in enumerate(bed_report['crossfades'],1):
            center=item['center']
            start=max(0,center-3)
            source.seek(round(start*sr))
            samples=source.read(round(6*sr),dtype='float32',always_2d=True)
            assert len(samples)==6*sr
            wave=qa/f'crossfade-{number:02}.wav'
            sf.write(wave,samples,sr,subtype='FLOAT')
            pre=samples[round(.2*sr):round(1.7*sr)]
            post=samples[round(4.3*sr):round(5.8*sr)]
            jump=10*math.log10((power(post)+1e-12)/(power(pre)+1e-12))
            a=round((item['start']-start)*sr)
            b=round((item['end']-start)*sr)
            overlap=samples[a:b]
            ref=(power(pre)+power(post))/2
            overlap_change=10*math.log10((power(overlap)+1e-12)/(ref+1e-12))
            derivatives=abs(np.diff(samples,axis=0))
            natural_p999=float(np.quantile(derivatives,.999))
            edges=[float(np.max(abs(samples[i]-samples[i-1]))) for i in [a,b] if 0<i<len(samples)]
            step_ratio=max(edges)/(natural_p999+1e-12)
            lr=float(np.corrcoef(overlap[:,0],overlap[:,1])[0,1])
            # Compare stereo fold-down against equal-energy stereo, not against absolute source volume.
            fold_loss=10*math.log10((power(overlap.mean(axis=1))+1e-12)/(power(overlap)+1e-12))
            label=f'crossfade {number:02}  center {center:.3f}s  overlap {item["start"]:.3f}-{item["end"]:.3f}s'
            left=(item['start']-start)/6*1200
            right=(item['end']-start)/6*1200
            graph=('[0:a]asplit=2[a][b];[a]showwavespic=s=1200x200:split_channels=1:colors=orange|cyan[w];'
                   '[b]showspectrumpic=s=1200x360:legend=0:color=viridis:scale=log:fscale=log:stop=12000:drange=80[s];'
                   f"[w][s]vstack=inputs=2,pad=1200:600:0:0:black,drawbox=x={left:.2f}:y=0:w=1:h=560:color=white@0.6:t=fill,"
                   f"drawbox=x={right:.2f}:y=0:w=1:h=560:color=white@0.6:t=fill,"
                   f"drawtext=text='{label}':fontcolor=white:fontsize=19:x=8:y=570[v]")
            run(['-loglevel','error','-i',wave,'-filter_complex',graph,'-map','[v]','-frames:v','1',
                 '-threads','1',qa/f'crossfade-{number:02}.png'])
            result={'number':number,'start':item['start'],'center':center,'end':item['end'],'qa_clip':str(wave),
                    'energy_jump_db_pre_post_1_5s':jump,'overlap_mean_power_change_db':overlap_change,
                    'edge_sample_step_vs_local_derivative_p999':step_ratio,
                    'stereo_correlation':lr,'mono_fold_energy_change_db':fold_loss,
                    'passed':abs(jump)<3 and abs(overlap_change)<3 and step_ratio<1.5 and fold_loss>-4}
            loops.append(result)
            print('LOOP',number,'step',round(jump,3),'fade',round(overlap_change,3),'edge_ratio',round(step_ratio,3),'fold',round(fold_loss,3),flush=True)
    run(['-loglevel','error','-framerate','1','-start_number','1','-i',qa/'crossfade-%02d.png',
         '-vf',f'tile=1x{len(loops)}:nb_frames={len(loops)}:padding=3:margin=3',
         '-frames:v','1','-threads','1',qa/'crossfades-contact.png'])
    source_integrity=all(checksum(path)==value for path,value in report['source_checksums'].items())
    assert source_integrity
    result={'video':str(Path(args.video).resolve()),'duration_s':duration,'duration_difference_s':float(after['format']['duration'])-duration,
            'metadata':after,'video_copy':True,'video_stream_sha256':copied_hash,'decode_errors':False,
            'integrated_lufs':float(stats['input_i']),'true_peak_dbtp':float(stats['input_tp']),
            'music_intro_lufs':intro,'music_speech_lufs':report['music_levels_intro_speech_outro'][1],
            'music_outro_lufs':outro,'speech_windows':speech,'crossfades':loops,
            'source_integrity_unchanged':source_integrity,'visual_spectral_review':'pending',
            'notes':['Loop WAVs are the unattenuated music-only bed, center ±3 seconds.',
                     'Energy step: 1.5-second RMS windows outside the overlap; native beat transients are not level steps.',
                     'No global normalization applied to approved speech/SFX; original bus gain is 0dB.']}
    write_json(qa/'music-qa.json',result)
    assert all(x['passed'] for x in loops),loops
    assert all(x['passed'] for x in speech),speech
    assert abs(intro+20)<.25 and abs(outro+20)<.25
    assert abs(result['music_speech_lufs']+33)<.25
    print(json.dumps({k:result[k] for k in ['duration_s','integrated_lufs','true_peak_dbtp','music_intro_lufs',
                                           'music_speech_lufs','music_outro_lufs','speech_windows']},indent=2),flush=True)


if __name__=='__main__':
    main()
