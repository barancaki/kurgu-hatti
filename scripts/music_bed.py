"""Build a bar-aligned instrumental bed with one source intro and one native outro."""
import argparse
import math
from pathlib import Path

import numpy as np
from scipy import ndimage, signal
import soundfile as sf

from common import output_path, write_json
from render_common import run


def analyze(samples, sr, bpm_hint=None):
    mono = signal.resample_poly(samples.mean(axis=1), 1, sr//12000)
    fs = sr/(sr//12000)
    frequencies, times, stft = signal.stft(mono, fs, nperseg=1024, noverlap=904)
    hop = 120/fs
    magnitude = abs(stft)
    flux = np.r_[0, np.maximum(0,np.diff(np.log1p(100*magnitude),axis=1)).sum(axis=0)]
    onset = np.maximum(0,ndimage.gaussian_filter1d(flux,1)-ndimage.median_filter(flux,size=51))
    onset /= max(float(np.percentile(onset,95)),1e-9)
    if not np.any(onset>0):
        raise ValueError('No rhythmic onsets detected; choose a rhythmic instrumental')
    centered = onset-onset.mean()
    ac = signal.correlate(centered,centered,method='fft')[len(centered)-1:]
    lo,hi = (bpm_hint*.85,bpm_hint*1.15) if bpm_hint else (60,160)
    bpms = np.arange(lo,hi,.005)
    periods = 60/bpms
    scores = sum(np.interp(k*periods/hop,np.arange(len(ac)),ac)/math.sqrt(k) for k in range(1,9))
    beat = float(periods[np.argmax(scores)])
    phases = np.arange(0,beat,.002)
    phase = float(phases[np.argmax([np.interp(np.arange(p,times[-1]-2,beat),times,onset).mean() for p in phases])])
    bar = 4*beat
    halfsec = round(sr*.5)
    energy = np.sqrt(np.mean(samples[:len(samples)//halfsec*halfsec].reshape(-1,halfsec,2)**2,axis=(1,2)))
    db = 20*np.log10(energy+1e-12)
    middle = float(np.median(db[len(db)//5:len(db)*4//5]))
    eligible = [i for i in range(len(db)-10) if db[i]>middle-3 and np.mean(db[i:i+10]>middle-3)>=.8]
    onset_time = eligible[0]*.5 if eligible else min(8*bar,len(samples)/sr*.15)
    intro_beat = max(0,round((onset_time-phase)/beat))
    downbeat = (phase+intro_beat*beat)%bar
    intro_end = downbeat+max(2,round((onset_time-downbeat)/bar))*bar
    source_duration = len(samples)/sr
    tail_start = source_duration-8*bar
    outro_start = downbeat+round((tail_start-downbeat)/bar)*bar
    if outro_start-intro_end<12*bar:
        raise ValueError('Source needs at least twelve complete body bars between intro and outro')
    pitch_bins = np.zeros(len(frequencies),dtype=int)
    valid = (frequencies>=100)&(frequencies<=4000)
    pitch_bins[valid] = np.rint(69+12*np.log2(frequencies[valid]/440)).astype(int)%12
    chroma = np.array([(magnitude[valid&(pitch_bins==pc)]**2).sum(axis=0) for pc in range(12)])
    return {'bpm':60/beat,'beat_s':beat,'bar_s':bar,'downbeat_phase_s':downbeat,
            'intro_end_s':intro_end,'outro_start_s':outro_start,'source_duration_s':source_duration,
            'beat_confidence':float(np.interp(beat/hop,np.arange(len(ac)),ac)/ac[0]),
            'meter':'4/4 inferred from intro energy change and onset periodicity',
            '_times':times,'_chroma':chroma}


def feature(analysis, samples, sr, center, before=False):
    bar = analysis['bar_s']
    a,b = (center-bar,center) if before else (center,center+bar)
    mask = (analysis['_times']>=a)&(analysis['_times']<b)
    chroma = analysis['_chroma'][:,mask].mean(axis=1)
    chroma /= max(float(np.linalg.norm(chroma)),1e-12)
    chunk = samples[round(a*sr):round(b*sr)]
    db = float(20*np.log10(np.sqrt(np.mean(chunk**2))+1e-12))
    return chroma,db


def plan(analysis, samples, sr, duration):
    bar = analysis['bar_s']
    intro,outro,source_end = [analysis[k] for k in ['intro_end_s','outro_start_s','source_duration_s']]
    fade = min(2.,max(1.5,3*analysis['beat_s']))
    # shortcut: complete native intro/outro need enough room; use a shorter source for shorter edits.
    if duration<intro+(source_end-outro)+fade:
        raise ValueError('Target is too short to preserve this source intro and outro')
    extra_bars = round((duration-source_end)/bar)
    pieces = []
    # Leave an entire bar before the outro so even outgoing fade handles stay inside the body.
    first_exit=outro+min(extra_bars,-1)*bar
    pieces.append({'role':'intro-and-body','source_start':0.,'source_end':first_exit+fade/2,
                   'exit_bar_s':first_exit})
    entries = []
    remaining=max(0,extra_bars)+(extra_bars>=0)
    max_span = max(4,int(.60*round((outro-intro)/bar)))
    count = math.ceil(remaining/max_span) if remaining else 0
    spans = [remaining//count+(i<remaining%count) for i in range(count)] if count else []
    exit_time = first_exit
    for number,bars in enumerate(spans):
        candidates = np.arange(intro+bar,outro-bars*bar-bar/2,bar)
        if not len(candidates):
            raise ValueError('Not enough body bars for a safe insertion')
        left,level = feature(analysis,samples,sr,exit_time,True)
        ranked = []
        for start in candidates:
            right,right_level = feature(analysis,samples,sr,float(start))
            separation = min([abs(start-old)/bar for old in entries] or [99])
            score = 1-float(np.dot(left,right))+.05*abs(level-right_level)+max(0,6-separation)
            if number==len(spans)-1:
                ending,ending_level = feature(analysis,samples,sr,float(start+bars*bar),True)
                tail,tail_level = feature(analysis,samples,sr,outro)
                score += (1-float(np.dot(ending,tail)))+.04*abs(ending_level-tail_level)
            ranked.append((score,float(start)))
        score,start = min(ranked)
        entries.append(start)
        exit_time = start+bars*bar
        pieces.append({'role':'body-variation','source_start':start-fade/2,'source_end':exit_time+fade/2,
                       'entry_bar_s':start,'exit_bar_s':exit_time,'bars':int(bars),'match_cost':score})
    pieces.append({'role':'native-outro','source_start':outro-fade/2,'source_end':source_end,
                   'entry_bar_s':outro})
    assert all(p['source_end']<=outro for p in pieces if p['role']!='native-outro')
    assert len(entries)==len(set(round(t,4) for t in entries))
    return pieces,fade


def assemble(samples, sr, pieces, fade_s):
    fade = round(fade_s*sr)
    lengths = [round(p['source_end']*sr)-round(p['source_start']*sr) for p in pieces]
    result = np.zeros((sum(lengths)-fade*(len(pieces)-1),2),dtype='float32')
    theta = np.linspace(0,np.pi/2,fade,dtype='float64')
    outgoing,incoming = np.cos(theta),np.sin(theta)
    joins=[]
    end=0
    for i,piece in enumerate(pieces):
        clip = samples[round(piece['source_start']*sr):round(piece['source_end']*sr)]
        piece['gain_db']=0.
        if not i:
            result[:len(clip)] = clip
            start=0
        else:
            start=end-fade
            # Match phrase energy without flattening the native musical dynamics.
            pre=result[round(end-fade/2-2.8*sr):round(end-fade/2-1.3*sr)]
            post=clip[round(fade/2+1.3*sr):round(fade/2+2.8*sr)]
            jump=float(10*np.log10((np.mean(post**2)+1e-12)/(np.mean(pre**2)+1e-12)))
            piece['gain_db']=float(np.clip(jump,-2,2)-jump)
            clip=clip*10**(piece['gain_db']/20)
            left = result[start:end].copy()
            right = clip[:fade]
            rho = float(np.mean(left*right)/(np.sqrt(np.mean(left**2)*np.mean(right**2))+1e-12))
            # Compensate correlated material so equal-power overlaps cannot build a +3dB hump.
            norm = np.sqrt(np.maximum(.1,1+rho*np.sin(2*theta)))
            result[start:end] = (left*(outgoing/norm)[:,None]+right*(incoming/norm)[:,None]).astype('float32')
            result[end:start+len(clip)] = clip[fade:]
            joins.append({'start':start/sr,'center':(start+fade/2)/sr,'end':end/sr,
                          'outgoing_source_bar':pieces[i-1]['exit_bar_s'],
                          'incoming_source_bar':piece['entry_bar_s'],'correlation':rho,
                          'curve':'equal-power cosine/sine, overlap-correlation compensated'})
        end=start+len(clip)
        piece['unfitted_output_start']=start/sr
        piece['unfitted_output_end']=end/sr
    assert end==len(result) and np.isfinite(result).all()
    return result,joins


def build_bed(music, duration, output, json_output=None, bpm_hint=None):
    if not math.isfinite(duration) or duration<=0 or (bpm_hint is not None and not 40<=bpm_hint<=240):
        raise ValueError('Duration and tempo hint must be valid positive values')
    output = output_path(output,[music])
    report = output_path(json_output or output.with_suffix('.json'),[music,output])
    decoded = output_path(output.parent/'music-source.wav',[music,output,report])
    run(['-loglevel','error','-i',music,'-map','0:a:0','-ar','48000','-ac','2','-c:a','pcm_f32le',decoded])
    samples,sr = sf.read(decoded,dtype='float32',always_2d=True)
    if len(samples)<sr*30 or not np.isfinite(samples).all():
        raise ValueError('Need at least 30 seconds of finite stereo music')
    analysis = analyze(samples,sr,bpm_hint)
    pieces,fade = plan(analysis,samples,sr,duration)
    assembled,joins = assemble(samples,sr,pieces,fade)
    raw = output_path(output.parent/'music-bed-unfitted.wav',[music,output,report,decoded])
    sf.write(raw,assembled,sr,subtype='FLOAT')
    tempo_fit = (len(assembled)/sr)/duration
    if abs(tempo_fit-1)>.035:
        raise ValueError('Exact fit would change tempo by more than 3.5 percent')
    run(['-loglevel','error','-i',raw,'-af',f'atempo={tempo_fit:.12f},apad,atrim=end_sample={round(duration*sr)}',
         '-ar',sr,'-ac','2','-c:a','pcm_f32le',output])
    for p in pieces:
        p['output_start']=p['unfitted_output_start']/tempo_fit
        p['output_end']=p['unfitted_output_end']/tempo_fit
    for j in joins:
        for key in ['start','center','end']:
            j[key]/=tempo_fit
    meta={k:v for k,v in analysis.items() if not k.startswith('_')}
    meta.update({'source':str(Path(music).resolve()),'output':str(output),'duration_s':duration,'sample_rate':sr,
                 'tempo_fit_factor':tempo_fit,'pitch_preserved':True,'output_bpm':analysis['bpm']*tempo_fit,
                 'crossfade_s':fade/tempo_fit,'segments':pieces,'crossfades':joins,
                 'native_intro_count':1,'native_outro_count':1,
                 'boundary_assumption':'bar phase inferred from onset/energy; not a manually annotated score'})
    info=sf.info(output)
    assert info.frames==round(duration*sr) and info.channels==2 and info.samplerate==48000
    write_json(report,meta)
    print(f'BED {duration:.3f}s; {analysis["bpm"]:.3f} BPM; {len(joins)} crossfades; tempo fit {tempo_fit:.8f}',flush=True)
    return meta


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--music',required=True)
    parser.add_argument('--duration',type=float,required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--json')
    parser.add_argument('--bpm-hint',type=float)
    args=parser.parse_args()
    build_bed(args.music,args.duration,args.output,args.json,args.bpm_hint)


if __name__=='__main__':
    main()
