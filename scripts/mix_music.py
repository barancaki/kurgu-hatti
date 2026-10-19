"""Add a musical bed to approved video; copy video packets, encode only the audio."""
import argparse
import hashlib
import math
from pathlib import Path
import subprocess

import numpy as np
from scipy import signal
import soundfile as sf

from common import output_path, write_json
from music_bed import build_bed
from render_common import json_measure, probe, run
import settings


def checksum(path):
    digest=hashlib.sha256()
    with open(path,'rb') as source:
        for block in iter(lambda:source.read(4*1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def loudness(source, start=0, duration=None):
    args=['-ss',f'{start:.9f}','-i',source]
    trim=f'atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,' if duration is not None else ''
    return json_measure(run(args+['-vn','-af',trim+'loudnorm=I=-14:TP=-1:LRA=11:print_format=json','-f','null','-']))


def duck_envelope(key, sr, total, start, intro, outro, threshold=-24, ratio=3, maximum=5):
    hop=round(sr*.01)
    levels=np.zeros(math.ceil(total*sr/hop),dtype='float64')
    # Speech-band RMS prevents bass beds and high clicks from dominating a fallback key.
    sos=signal.butter(2,[100,5000],btype='bandpass',fs=sr,output='sos')
    filtered=signal.sosfilt(sos,key)
    n=len(filtered)//hop
    rms=np.sqrt(np.mean(filtered[:n*hop].reshape(-1,hop)**2,axis=1))
    first=round(start*sr/hop)
    if first<0:
        raise ValueError('Voice-key start must be nonnegative')
    length=min(n,max(0,len(levels)-first))
    levels[first:first+length]=rms[:length]
    times=(np.arange(len(levels))+.5)*hop/sr
    levels[(times<intro)|(times>=total-outro)]=0
    desired=np.minimum(maximum,np.maximum(0,(20*np.log10(levels+1e-12)-threshold)*(1-1/ratio)))
    result=np.empty(len(desired),dtype='float32')
    state=0.
    for i,value in enumerate(desired):
        coefficient=math.exp(-.01/(.03 if value>state else .4))
        state=coefficient*state+(1-coefficient)*value
        result[i]=state
    assert np.min(result)>=0 and np.max(result)<=maximum+1e-5
    return times,result


def gain_knots(times, total, intro, outro, gains):
    if total<intro+outro+2:
        raise ValueError('Need room for intro/outro and the two one-second ramps')
    knots=[0,intro,intro+1,total-outro-1,total-outro,total]
    values=[gains[0],gains[0],gains[1],gains[1],gains[2],gains[2]]
    result=np.empty_like(times,dtype='float64')
    for a,b,va,vb in zip(knots,knots[1:],values,values[1:]):
        mask=(times>=a)&(times<=b)
        u=np.clip((times[mask]-a)/(b-a),0,1)
        result[mask]=va+(vb-va)*(1-np.cos(np.pi*u))/2
    return result


def write_bus(bed, destination, sr, times, reduction, gains, intro, outro):
    total=len(bed)/sr
    gain_db=gain_knots(times,total,intro,outro,gains)-reduction
    with sf.SoundFile(destination,'w',samplerate=sr,channels=2,subtype='FLOAT') as writer:
        for start in range(0,len(bed),sr*10):
            end=min(len(bed),start+sr*10)
            t=np.arange(start,end,dtype='float64')/sr
            db=np.interp(t,times,gain_db)
            fade_in=np.sin(np.pi/2*np.clip(t/.4,0,1))**2
            fade_out=np.clip((total-t)/3,0,1)**2
            amplitude=(10**(db/20)*fade_in*fade_out).astype('float32')
            writer.write(bed[start:end]*amplitude[:,None])


def short_term(source,start,duration=10):
    log=run(['-ss',start,'-i',source,'-vn','-af',f'atrim=duration={duration},asetpts=PTS-STARTPTS,ebur128=peak=true','-f','null','-'])
    import re
    rows=re.findall(r'\bt:\s*([\d.]+)\s+TARGET:.*?\bS:\s*(-?[\d.]+)',log)
    values=np.array([float(s) for t,s in rows if float(t)>=3 and float(s)>-100])
    if not len(values):
        raise ValueError('No short-term loudness frames')
    return float(10*np.log10(np.mean(10**(values/10))))


def video_hash(source):
    return subprocess.check_output(['ffmpeg','-v','error','-i',str(source),'-map','0:v:0',
                                    '-c:v','copy','-f','streamhash','-hash','sha256','-'],text=True).strip()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,help='Approved video; audio already at the target loudness')
    parser.add_argument('--music',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--work-dir',default='work')
    parser.add_argument('--intro',type=float,default=5)
    parser.add_argument('--outro',type=float,default=4)
    parser.add_argument('--voice-key',help='Optional clean speech WAV/video, without SFX/music')
    parser.add_argument('--key-start',type=float,help='Key offset on final timeline; default intro duration')
    parser.add_argument('--bpm-hint',type=float)
    parser.add_argument('--speech-lufs',type=float,help='Music alone AFTER ducking (default: music.speech_lufs)')
    parser.add_argument('--title-lufs',type=float,help='Music alone in intro and outro (default: music.title_lufs)')
    settings.add_config_arg(parser)
    args=parser.parse_args()
    music_cfg=settings.load(args.config)['music']
    args.speech_lufs=music_cfg['speech_lufs'] if args.speech_lufs is None else args.speech_lufs
    args.title_lufs=music_cfg['title_lufs'] if args.title_lufs is None else args.title_lufs
    if not all(math.isfinite(x) for x in [args.intro,args.outro,args.speech_lufs,args.title_lufs]) or min(args.intro,args.outro)<=0:
        parser.error('Intro/outro lengths must be positive finite seconds')
    inputs=[args.input,args.music,args.voice_key]
    out=output_path(args.output,inputs)
    work=output_path(Path(args.work_dir)/'music-mix.json',inputs+[out]).parent
    for name in ['music-source.wav','music-bed-unfitted.wav','music-bed.wav','music-bed.json',
                 'approved-audio.wav','speech-key.wav','music-shaped.wav','music-bus.wav',
                 'music-mix-unlimited.wav','music-aac-check.m4a','music-master-limited-1.wav',
                 'music-master-limited-2.wav','music-ducking.json','music-mix.json','music-mux.log']:
        output_path(work/name,inputs+[out])
    total=float(probe(args.input)['format']['duration'])
    if total<=args.intro+args.outro+2:
        parser.error('Input is too short for the requested intro/outro ramps')
    originals={str(Path(p).resolve()):checksum(p) for p in inputs if p}
    bed_path=work/'music-bed.wav'
    bed_meta=build_bed(args.music,total,bed_path,work/'music-bed.json',args.bpm_hint)
    bed,sr=sf.read(bed_path,dtype='float32',always_2d=True)
    original_audio=work/'approved-audio.wav'
    run(['-loglevel','error','-i',args.input,'-map','0:a:0','-vn','-ar',sr,'-ac','2','-c:a','pcm_f32le',original_audio])
    if args.voice_key:
        keyfile=work/'speech-key.wav'
        run(['-loglevel','error','-i',args.voice_key,'-map','0:a:0','-vn','-ac','1','-ar',sr,'-c:a','pcm_f32le',keyfile])
        key,_=sf.read(keyfile,dtype='float32')
        key_start=args.intro if args.key_start is None else args.key_start
        key_description='clean speech'
    else:
        key,_=sf.read(original_audio,dtype='float32',always_2d=True)
        key=key.mean(axis=1)
        key_start=0
        key_description='approved audio speech-band fallback; may also react to SFX'
    times,reduction=duck_envelope(key,sr,total,key_start,args.intro,args.outro)
    del key
    # Hold the last gain through the very last sample, not just the final 10ms control block.
    times=np.r_[0,times,total]
    reduction=np.r_[reduction[0],reduction,reduction[-1]]
    unity=work/'music-shaped.wav'
    write_bus(bed,unity,sr,times,reduction,[0,0,0],args.intro,args.outro)
    regions=[(0,args.intro),(args.intro,total-args.outro-args.intro),(total-args.outro,args.outro)]
    measured=[loudness(unity,t,d) for t,d in regions]
    targets=[args.title_lufs,args.speech_lufs,args.title_lufs]
    gains=[target-float(stats['input_i']) for target,stats in zip(targets,measured)]
    if not np.isfinite(gains).all() or max(abs(x) for x in gains)>35:
        raise ValueError('Source intro/outro is too quiet for safe section calibration')
    music_bus=work/'music-bus.wav'
    for calibration in range(3):
        write_bus(bed,music_bus,sr,times,reduction,gains,args.intro,args.outro)
        music_levels=[loudness(music_bus,t,d) for t,d in regions]
        errors=[target-float(level['input_i']) for target,level in zip(targets,music_levels)]
        print('MUSIC CALIBRATION',calibration,[x['input_i'] for x in music_levels],flush=True)
        if max(abs(error) for error in errors)<=.15:
            break
        gains=[gain+error for gain,error in zip(gains,errors)]
    else:
        raise ValueError('Cannot calibrate the three music regions within 0.15 LU')
    del bed
    with sf.SoundFile(original_audio) as source, sf.SoundFile(music_bus) as music, \
         sf.SoundFile(work/'music-mix-unlimited.wav','w',samplerate=sr,channels=2,subtype='FLOAT') as writer:
        remaining=round(total*sr)
        while remaining:
            n=min(remaining,sr*10)
            voice=source.read(n,dtype='float32',always_2d=True)
            bedblock=music.read(n,dtype='float32',always_2d=True)
            if len(voice)<n:
                voice=np.pad(voice,((0,n-len(voice)),(0,0)))
            assert len(bedblock)==n
            writer.write(voice+bedblock)
            remaining-=n
    mixed=work/'music-mix-unlimited.wav'
    encoded=work/'music-aac-check.m4a'
    attempts=[]
    mastering=mixed
    for attempt in range(3):
        limiter_db=None
        if attempt:
            limiter_db=-1.4-.3*(attempt-1)
            mastering=work/f'music-master-limited-{attempt}.wav'
            run(['-loglevel','error','-i',mixed,'-af',
                 f'aresample=192000,alimiter=limit={10**(limiter_db/20):.12f}:attack=5:release=80:level=false:latency=1,aresample=48000',
                 '-t',total,'-c:a','pcm_f32le',mastering])
        run(['-loglevel','error','-i',mastering,'-map','0:a:0','-c:a','aac','-b:a','192k','-ar',sr,'-ac','2','-t',total,encoded])
        stats=loudness(encoded)
        attempts.append({'limiter_ceiling_dbtp':limiter_db,'aac_loudness':stats})
        print('AUDIO',attempt,stats['input_i'],stats['input_tp'],flush=True)
        if abs(float(stats['input_i'])+14)<=.5 and float(stats['input_tp'])<=-1:
            break
    else:
        raise ValueError('Cannot meet final LUFS/TP while retaining unity original-audio gain')
    run(['-loglevel','error','-i',args.input,'-i',encoded,'-map','0:v:0','-map','1:a:0',
         '-map_metadata','0','-c','copy','-t',total,'-movflags','+faststart',out],work/'music-mux.log')
    metadata=probe(out)
    input_video=next(s for s in probe(args.input)['streams'] if s['codec_type']=='video')
    output_video=next(s for s in metadata['streams'] if s['codec_type']=='video')
    assert all(input_video.get(k)==output_video.get(k) for k in ['width','height','r_frame_rate','codec_name','nb_frames'])
    assert abs(float(metadata['format']['duration'])-total)<=.05
    original_hash=video_hash(args.input)
    copied_hash=video_hash(out)
    assert original_hash==copied_hash,'Video packets differ'
    assert all(checksum(p)==value for p,value in originals.items()),'Protected input changed'
    control=[{'time':round(float(t),3),'reduction_db':round(float(d),4)} for t,d in zip(times[::10],reduction[::10])]
    write_json(work/'music-ducking.json',{'key':args.voice_key or args.input,'key_kind':key_description,'key_start_s':key_start,
               'ratio':3,'threshold_dbfs_rms':-24,'attack_ms':30,'release_ms':400,'max_extra_reduction_db':5,
               'measured_max_reduction_db':float(reduction.max()),'controls_100ms':control})
    result={'output':str(out),'input':str(Path(args.input).resolve()),'duration_s':total,'metadata':metadata,
            'source_checksums':originals,'video_stream_hash_v1':original_hash,'video_stream_hash_v2':copied_hash,
            'video_copy':True,'original_audio_gain_db':0,'music_bus':str(music_bus.resolve()),
            'speech_music_target_after_duck_lufs':args.speech_lufs,'title_music_target_lufs':args.title_lufs,
            'section_gain_db':gains,'music_levels_intro_speech_outro':[float(x['input_i']) for x in music_levels],
            'intro_s':args.intro,'outro_s':args.outro,'ramps_s':1,'fade_in_s':.4,'fade_out_s':3,
            'ducking_max_db':float(reduction.max()),'mastering_attempts':attempts,
            'final_integrated_lufs':float(stats['input_i']),'final_true_peak_dbtp':float(stats['input_tp']),
            'bed_report':str((work/'music-bed.json').resolve())}
    write_json(work/'music-mix.json',result)
    print(f'COMPLETE {out}; video copied; {total:.3f}s; I={stats["input_i"]}, TP={stats["input_tp"]}',flush=True)


if __name__=='__main__':
    main()
