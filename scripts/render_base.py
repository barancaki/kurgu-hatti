"""Render a webcam/screen base edit; all layout times are webcam seconds.

Paths inside layout.json are relative to the current project working directory.
Video uses a global 30fps frame grid. Audio uses the exact EDL sample timeline;
12ms crossfades use discarded source handles and never shorten the edit.
"""
import argparse
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
from pathlib import Path
import subprocess

import av
import numpy as np
from scipy import ndimage
import soundfile as sf

from common import output_path, write_json
from render_common import concat_file, encoder, json_measure, probe, run, video_options
import settings
from timemap import TimeMap, source_words

RENDER_SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def sync_steps(sync):
    local = sync.get('local_drift') or {}
    if local.get('pattern') == 'piecewise_constant_steps':
        plateaus = local['plateaus']
        boundaries = [sum(t['webcam_transition_bracket_s'])/2 for t in local['transition_brackets']]
        if len(plateaus) != len(boundaries)+1:
            raise ValueError('Inconsistent piecewise sync data')
        return boundaries, [p['offset_s'] for p in plateaus]
    return [], [sync['screen_offset_s']]


def offset_at(webcam_time, sync):
    boundaries, offsets = sync_steps(sync)
    if boundaries:
        return offsets[bisect_right(boundaries, webcam_time)]
    model = sync.get('offset_model', {})
    return model.get('intercept_s', offsets[0]) + model.get('slope_s_per_s', 0)*webcam_time


def make_plan(edl, layout, transcript, sync, cut_points, preview=False):
    fps = layout.get('fps', 30)
    width, height = (960, 540) if preview else (1920, 1080)
    mapping = TimeMap(edl['keep'], edl['raw_duration_s'])
    starts = [w['start'] for w in source_words(transcript)]
    modes = []
    for requested in layout['modes']:
        boundary = requested['start']
        snapped = 0. if boundary == 0 else min(starts, key=lambda t: abs(t-boundary))
        modes.append({'mode': requested['mode'], 'requested_webcam_start': boundary,
                      'webcam_start': snapped, 'output_start': mapping.to_output(snapped)})
    if modes[0]['mode'] != 'TALK' or any(a['output_start'] >= b['output_start'] for a,b in zip(modes,modes[1:])):
        raise ValueError('Modes must start with TALK and advance on the output timeline')
    for i, item in enumerate(modes):
        item['output_end'] = modes[i+1]['output_start'] if i+1<len(modes) else mapping.duration
    zooms = []
    for item in layout['zooms']:
        if item.get('skip'):
            continue
        start, end = mapping.to_output(item['start']), mapping.to_output(item['end'])
        if end > start:
            zooms.append(dict(item, output_start=start, output_end=end,
                              output_keyframes=[dict(k, output_time=mapping.to_output(k['time'])) for k in item.get('keyframes', [])]))
    changes = [{'output_start': m['output_start'], 'output_end': m['output_start']+layout['transition_s'],
                'from': modes[i-1]['mode'], 'to': m['mode']} for i,m in enumerate(modes) if i]
    frames = round(mapping.duration*fps)
    boundaries = {0, frames}
    for item in mapping.keep:
        boundaries.update([round(item['output_start']*fps), round(item['output_end']*fps)])
    for item in changes:
        boundaries.update([round(item['output_start']*fps), round(item['output_end']*fps)])
    step_times, _ = sync_steps(sync)
    boundaries.update(round(mapping.to_output(t)*fps) for t in step_times)
    # Keep long runs manageable without resetting their slow push.
    for item in mapping.keep:
        boundaries.update(range(round(item['output_start']*fps)+fps*30, round(item['output_end']*fps), fps*30))
    boundaries = sorted(b for b in boundaries if 0<=b<=frames)
    pieces = []
    cut_times = cut_points['output_times']
    for f0, f1 in zip(boundaries, boundaries[1:]):
        if f1<=f0:
            continue
        mid = (f0+f1)/(2*fps)
        ki = next(i for i,k in enumerate(mapping.keep) if k['output_start']-1e-7 <= mid < k['output_end']+1e-7)
        keep = mapping.keep[ki]
        t0 = f0/fps
        webcam_start = keep['start']+t0-keep['output_start']
        current = next(m for m in modes if m['output_start']<=mid<m['output_end'])
        transition = next((c for c in changes if c['output_start']<=mid<c['output_end']), None)
        talk_mode = current
        if transition and transition['to']=='SCREEN':
            talk_mode = modes[modes.index(current)-1]
        anchor = max(keep['output_start'], talk_mode['output_start'])
        talk_end = min(keep['output_end'], talk_mode['output_end'])
        if transition and transition['to']=='SCREEN':
            talk_end = transition['output_start']
        span = max(talk_end-anchor, 1/fps)
        count = bisect_right(cut_times, max(t0,keep['output_start'])+1e-7)
        base_zoom = layout['talk']['zoom_levels'][count%2]
        zoom0 = base_zoom+layout['talk']['push']*max(0,t0-anchor)/span
        zoom_rate = layout['talk']['push']/span
        pieces.append({'index':len(pieces), 'frame_start':f0, 'frames':f1-f0, 'output_start':t0,
                       'duration':(f1-f0)/fps, 'webcam_start':webcam_start,
                       'screen_start':webcam_start-offset_at(webcam_start+(f1-f0)/(2*fps),sync),
                       'keep_index':ki, 'mode':current['mode'], 'transition':transition,
                       'talk_zoom0':zoom0, 'talk_zoom_rate':zoom_rate})
    assert sum(p['frames'] for p in pieces)==frames
    return {'width':width,'height':height,'fps':fps,'frames':frames,'duration':frames/fps,
            'edl_duration':mapping.duration,'modes':modes,'zooms':zooms,'transitions':changes,
            'pieces':pieces,'sync_step_midpoints_webcam_s':step_times}


def keyframe_expression(zoom, field, time_expression, axis=None):
    keys = zoom.get('output_keyframes', [])
    value = lambda key: key[field][axis] if axis is not None else key[field]
    if not keys:
        return str(zoom[field][axis] if axis is not None else zoom[field])
    expression = f'{value(keys[-1]):.9f}'
    for left,right in reversed(list(zip(keys,keys[1:]))):
        a,b = left['output_time'],right['output_time']
        if b<=a:
            continue
        progress = f'clip(({time_expression}-{a:.9f})/{b-a:.9f},0,1)'
        blend = f'((1-cos(PI*({progress})))/2)'
        changing = f'({value(left):.9f}+({value(right)-value(left):.9f})*{blend})'
        expression = f'if(lt({time_expression},{b:.9f}),{changing},{expression})'
    return expression


def screen_filter(piece, layout, plan):
    x,y,cw,ch = layout['screen_crop']
    w,h,fps = plan['width'],plan['height'],plan['fps']
    t = f'({piece["output_start"]:.9f}+on/{fps})'
    weights = []
    for z in plan['zooms']:
        a,b = z['output_start'],z['output_end']
        ease = min(layout['zoom_ease_s'],(b-a)/2)
        weight = f'if(lt({t},{a:.9f}),0,if(lt({t},{a+ease:.9f}),(1-cos(PI*({t}-{a:.9f})/{ease:.9f}))/2,if(lt({t},{b-ease:.9f}),1,if(lt({t},{b:.9f}),(1+cos(PI*({t}-{b-ease:.9f})/{ease:.9f}))/2,0))))'
        weights.append((z,weight))
    zoom = '1'+''.join(f'+(({keyframe_expression(z,"scale",t)})-1)*({p})' for z,p in weights)
    cx = '.5'+''.join(f'+((({keyframe_expression(z,"center",t,0)})-{x})/{cw}-.5)*({p})' for z,p in weights)
    cy = '.5'+''.join(f'+((({keyframe_expression(z,"center",t,1)})-{y})/{ch}-.5)*({p})' for z,p in weights)
    reframed = [(z,p) for z,p in weights if z.get('output_anchor') and
                z['output_start'] < piece['output_start']+piece['duration'] and z['output_end'] > piece['output_start']]
    if reframed:
        # Extend the UI ground below the capture so bottom targets can move above the PiP.
        active = '+'.join(f'({p})' for z,p in reframed)
        shift = '+'.join(f'({z["output_anchor"][1]}-.5)*{h*2}/zoom*({p})' for z,p in reframed)
        pos = f'({h}*({cy})-{h}/zoom)'
        bounded = f'min(max({pos},0),{h}-{h*2}/zoom)'
        ypos = f'({bounded})*(1-({active}))+({pos}-({shift}))*({active})'
        cutoff = round((min(z['mask_below'] for z,p in reframed)-y)/ch*h)
        enabled = active.replace(f'on/{fps}', 't')
        background = reframed[0][0].get('background', settings.load()['colors']['screen_fill'])
        return (f'fps={fps},setpts=N/({fps}*TB),crop={cw}:{ch}:{x}:{y},'
                f'scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},'
                f"format=rgb24,drawbox=x=0:y={cutoff}:w=iw:h=ih-{cutoff}:color={background}:t=fill:enable='gt({enabled},0)',"
                f'pad={w*2}:{h*2}:0:0:color={background},'
                f"zoompan=z='2*({zoom})':x='min(max({w}*({cx})-{w}/zoom,0),{w}-{w*2}/zoom)':"
                f"y='{ypos}':d=1:s={w}x{h}:fps={fps},setsar=1")
    return (f'fps={fps},setpts=N/({fps}*TB),crop={cw}:{ch}:{x}:{y},'
            f'scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},'
            f"zoompan=z='{zoom}':x='min(max(iw*({cx})-iw/zoom/2,0),iw-iw/zoom)':"
            f"y='min(max(ih*({cy})-ih/zoom/2,0),ih-ih/zoom)':d=1:s={w}x{h}:fps={fps},setsar=1")


def png(path, pixels):
    h,w = pixels.shape[:2]
    pix = 'rgba' if pixels.ndim==3 else 'gray'
    result = subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo',
                             '-pix_fmt',pix,'-s',f'{w}x{h}','-i','pipe:0','-frames:v','1',
                             '-threads','1',str(path)],input=pixels.tobytes(),capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace'))


def pip_assets(work, layout, plan):
    scale = plan['width']/1920
    diameter = round(layout['pip']['diameter']*scale)
    border = max(1,round(layout['pip']['border']*scale))
    inner = diameter-2*border
    yy,xx = np.mgrid[:inner,:inner]
    mask = np.clip(inner/2-np.hypot(xx-(inner-1)/2,yy-(inner-1)/2),0,1)*255
    pad = round(20*scale)
    side = diameter+2*pad
    yy,xx = np.mgrid[:side,:side]
    radius = np.hypot(xx-(side-1)/2,yy-(side-1)/2)
    ring = np.clip(diameter/2-radius,0,1)-np.clip(inner/2-radius,0,1)
    shadow_circle = (np.hypot(xx-(side-1)/2-4*scale,yy-(side-1)/2-7*scale)<diameter/2).astype('float32')
    shadow = ndimage.gaussian_filter(shadow_circle,6*scale)*.32
    alpha = ring+shadow*(1-ring)
    rgb = np.divide(ring,alpha,out=np.zeros_like(alpha),where=alpha>0)
    asset = np.dstack([rgb*255,rgb*255,rgb*255,alpha*255]).clip(0,255).astype('uint8')
    mask_path, ring_path = work/f'pip-mask-{diameter}.png',work/f'pip-ring-{diameter}.png'
    png(mask_path,mask.astype('uint8'));png(ring_path,asset)
    return mask_path,ring_path


def input_args(source, start):
    if start<0:
        raise ValueError('Requested video before source start')
    return ['-ss',f'{start:.9f}','-threads','2','-i',str(source)]


def read_frame(process, width, height):
    raw = process.stdout.read(width*height*3)
    if len(raw)!=width*height*3:
        raise RuntimeError('Unexpected video decoder EOF')
    return np.frombuffer(raw,dtype=np.uint8).reshape(height,width,3)


def transition_frame(cam, screen, progress, zoom, layout, plan):
    """A short rounded-rectangle morph, resized with installed libswscale/PyAV."""
    w,h = plan['width'],plan['height']
    scale = w/1920
    e = (1-math.cos(math.pi*min(1,max(0,progress))))/2
    d = layout['pip']['diameter']*scale
    margin = layout['pip']['margin']*scale
    tw,th = round(w+(d-w)*e),round(h+(d-h)*e)
    tx,ty = round((w-margin-d)*e),round((h-margin-d)*e)
    sx,sy = layout['face']['talk_anchor']
    center0 = np.array([1920/zoom/2+(1920-1920/zoom)*sx,
                        1080/zoom/2+(1080-1080/zoom)*sy])
    center = center0*(1-e)+np.array(layout['face']['center'])*e
    ch = (1080/zoom)*(1-e)+layout['face']['pip_source_size']*e
    cw = ch*tw/th
    factor = min(1,1920/cw,1080/ch)
    cw,ch = cw*factor,ch*factor
    x = round(min(max(center[0]-cw/2,0),1920-cw))
    y = round(min(max(center[1]-ch/2,0),1080-ch))
    cw,ch = max(2,round(cw)),max(2,round(ch))
    tile = av.VideoFrame.from_ndarray(np.ascontiguousarray(cam[y:y+ch,x:x+cw]),format='rgb24').reformat(tw,th).to_ndarray(format='rgb24')
    yy,xx = np.mgrid[:th,:tw]
    radius = e*min(tw,th)/2
    qx,qy = np.abs(xx-(tw-1)/2)-tw/2+radius,np.abs(yy-(th-1)/2)-th/2+radius
    distance = np.hypot(np.maximum(qx,0),np.maximum(qy,0))+np.minimum(np.maximum(qx,qy),0)-radius
    outer = np.clip(.5-distance,0,1).astype('float32')
    border = layout['pip']['border']*scale*e
    white = np.clip(distance+border+.5,0,1)[...,None]
    tile = tile*(1-white)+255*white
    canvas = screen.copy()
    if e>.001:
        fullmask = np.zeros((h,w),dtype='float32')
        fullmask[ty:ty+th,tx:tx+tw] = outer
        fullmask = ndimage.shift(fullmask,(7*scale*e,4*scale*e),order=1,mode='constant')
        shadow = ndimage.gaussian_filter(fullmask,max(.1,6*scale*e))*.32*e
        canvas = (canvas*(1-shadow[...,None])).astype('uint8')
    alpha = outer[...,None]
    canvas[ty:ty+th,tx:tx+tw] = (tile*alpha+canvas[ty:ty+th,tx:tx+tw]*(1-alpha)).clip(0,255).astype('uint8')
    return canvas


def render_transition(piece, layout, plan, enc, target, log):
    w,h,fps = plan['width'],plan['height'],plan['fps']
    n = piece['frames']
    args = ['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-threads','2']
    cam = subprocess.Popen(args+input_args(layout['sources']['webcam'],piece['webcam_start'])+
                           ['-vf',f'fps={fps}','-frames:v',str(n),'-an','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                           stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    screen = subprocess.Popen(args+input_args(layout['sources']['screen'],piece['screen_start'])+
                              ['-vf',screen_filter(piece,layout,plan),'-frames:v',str(n),
                               '-an','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                              stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    with open(log,'wb') as errors:
        writer = subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y',
                                   '-f','rawvideo','-pix_fmt','rgb24','-s',f'{w}x{h}','-r',str(fps),
                                   '-i','pipe:0','-an']+video_options(enc,fps)+['-frames:v',str(n),str(target)],
                                  stdin=subprocess.PIPE,stderr=errors)
        try:
            transition = piece['transition']
            for i in range(n):
                t = piece['output_start']+i/fps
                p = (t-transition['output_start'])/(transition['output_end']-transition['output_start'])
                # Include both exact endpoint shapes on the quantized frame interval.
                p = i/max(1,n-1) if abs(piece['output_start']-transition['output_start'])<1/fps else p
                if transition['to']=='TALK':
                    p = 1-p
                frame = transition_frame(read_frame(cam,1920,1080),read_frame(screen,w,h),p,
                                         piece['talk_zoom0']+piece['talk_zoom_rate']*i/fps,layout,plan)
                writer.stdin.write(frame.tobytes())
            writer.stdin.close()
            if writer.wait():
                raise RuntimeError(Path(log).read_text(errors='replace')[-3000:])
            if cam.wait() or screen.wait():
                raise RuntimeError('Transition input decoder failed')
        finally:
            for process in [cam,screen,writer]:
                if process.poll() is None:
                    process.kill()
                process.wait()


def render_piece(piece, layout, plan, enc, work, assets):
    w,h,fps = plan['width'],plan['height'],plan['fps']
    source_stats = [(str(p), Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p in layout['sources'].values()]
    payload = json.dumps([piece,layout,plan['zooms'],w,h,enc,source_stats,RENDER_SOURCE_HASH],sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()[:12]
    target = work/f'{piece["index"]:03}-{digest}.mp4'
    log = target.with_suffix('.log')
    if target.exists():
        try:
            stream = next(s for s in probe(target)['streams'] if s['codec_type']=='video')
            if int(stream.get('nb_frames',0))==piece['frames'] and stream['width']==w:
                return target
        except (ValueError,RuntimeError,subprocess.CalledProcessError,StopIteration):
            pass
    if piece['transition']:
        render_transition(piece,layout,plan,enc,target,log)
    elif piece['mode']=='TALK':
        fx,fy = layout['face']['talk_anchor']
        filt = (f'fps={fps},setpts=N/({fps}*TB),'
                f'zoompan=z=\'{piece["talk_zoom0"]:.9f}+{piece["talk_zoom_rate"]:.9f}*on/{fps}\':'
                f'x=\'(iw-iw/zoom)*{fx}\':y=\'(ih-ih/zoom)*{fy}\':d=1:s={w}x{h}:fps={fps},setsar=1')
        run(['-loglevel','error']+input_args(layout['sources']['webcam'],piece['webcam_start'])+
            ['-an','-vf',filt,'-frames:v',piece['frames']]+video_options(enc,fps)+[target],log)
    else:
        factor = w/1920
        diameter = round(layout['pip']['diameter']*factor)
        border = max(1,round(layout['pip']['border']*factor))
        inner = diameter-2*border
        margin = round(layout['pip']['margin']*factor)
        pad = round(20*factor)
        px,py = w-margin-diameter,h-margin-diameter
        side = layout['face']['pip_source_size']*(inner/diameter)
        cx,cy = layout['face']['center']
        graph = (f'[0:v]fps={fps},setpts=N/({fps}*TB),crop={round(side)}:{round(side)}:{round(cx-side/2)}:{round(cy-side/2)},'
                 f'scale={inner}:{inner}:flags=lanczos,setsar=1[face];'
                 f'[2:v]format=gray[mask];[face][mask]alphamerge[pip];'
                 f'[1:v]{screen_filter(piece,layout,plan)}[screen];'
                 f'[screen][3:v]overlay={px-pad}:{py-pad}:shortest=1[border];'
                 f'[border][pip]overlay={px+border}:{py+border}:shortest=1,format=yuv420p[v]')
        graph_path = target.with_suffix('.ffgraph')
        graph_path.write_text(graph,encoding='utf-8')
        args = ['-loglevel','error','-filter_complex_threads','2']+input_args(layout['sources']['webcam'],piece['webcam_start'])
        args += input_args(layout['sources']['screen'],piece['screen_start'])
        args += ['-loop','1','-i',assets[0],'-loop','1','-i',assets[1],
                 '-filter_complex_script',graph_path,'-map','[v]','-an','-frames:v',piece['frames']]
        run(args+video_options(enc,fps)+[target],log)
    return target


def edit_audio(source, edl, work, crossfade_s=.012):
    sr = 48000
    mono = work/'webcam-mono.wav'
    run(['-loglevel','error','-i',source,'-vn','-af','pan=mono|c0=0.5*c0+0.5*c1',
         '-ar',sr,'-c:a','pcm_f32le',mono],work/'audio-decode.log')
    samples,_ = sf.read(mono,dtype='float32')
    mapping = TimeMap(edl['keep'],edl['raw_duration_s'])
    result = np.zeros(round(mapping.duration*sr),dtype='float32')
    for keep in mapping.keep:
        a,b = round(keep['output_start']*sr),round(keep['output_end']*sr)
        indices = round(keep['start']*sr)+np.arange(b-a)
        result[a:b] = samples[np.clip(indices,0,len(samples)-1)]
    half = round(crossfade_s*sr/2)
    for before,after in zip(mapping.keep,mapping.keep[1:]):
        boundary = round(after['output_start']*sr)
        positions = np.arange(-half,half)
        previous = samples[np.clip(round(before['end']*sr)+positions,0,len(samples)-1)]
        following = samples[np.clip(round(after['start']*sr)+positions,0,len(samples)-1)]
        weight = np.linspace(0,1,len(positions),dtype='float32')
        result[boundary-half:boundary+half] = previous*(1-weight)+following*weight
    edited = work/'edited-master.wav'
    sf.write(edited,result,sr,subtype='FLOAT')
    return edited


def normalize_audio(edited, work):
    filters = subprocess.check_output(['ffmpeg','-hide_banner','-filters'],text=True)
    chain = 'highpass=f=80,afftdn=nf=-25:nr=6,acompressor=threshold=0.1:ratio=3:attack=10:release=120'
    if ' deesser ' in filters:
        chain += ',deesser=i=0.1:m=0.2:f=0.5'
    chain += ',pan=stereo|c0=c0|c1=c0'
    cfg = settings.load()
    target, ceiling = cfg['audio']['target_lufs'], cfg['audio']['true_peak_dbtp']
    # 0.7dB headroom protects the requested true-peak ceiling against AAC reconstruction overshoot.
    loud = settings.loudnorm(.7, cfg)
    measurement = run(['-i',edited,'-af',chain+','+loud+':print_format=json','-f','null','-'],work/'loudnorm-pass1.log')
    stats = json_measure(measurement)
    second = (loud+f':measured_I={stats["input_i"]}:measured_TP={stats["input_tp"]}'
              f':measured_LRA={stats["input_lra"]}:measured_thresh={stats["input_thresh"]}'
              f':offset={stats["target_offset"]}:linear=true:print_format=json')
    dest = work/'normalized.wav'
    log = run(['-i',edited,'-af',chain+','+second,'-ar','48000','-c:a','pcm_s24le',dest],work/'loudnorm-pass2.log')
    second_stats = json_measure(log)
    # Correct the dynamic normalizer's small LUFS residual while retaining AAC peak headroom.
    gain_db = min(target-.05-float(second_stats['output_i']), ceiling-.45-float(second_stats['output_tp']))
    calibrated = work/'normalized-calibrated.wav'
    run(['-loglevel','error','-i',dest,'-af',f'volume={gain_db:.6f}dB',
         '-ar','48000','-c:a','pcm_s24le',calibrated],work/'loudnorm-calibration.log')
    return calibrated,{'preprocessing':chain,'first_pass':stats,'second_pass':second_stats,
                       'target_integrated_lufs':target,'requested_true_peak_ceiling_dbtp':ceiling,
                       'normalizer_peak_limit_dbtp':ceiling-.7,'post_normalizer_gain_db':gain_db,
                       'calibrated_pcm_peak_ceiling_dbtp':ceiling-.45}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edl',required=True)
    parser.add_argument('--layout',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--work-dir',required=True)
    parser.add_argument('--preview',action='store_true',help='960x540 and fast encoder preset')
    parser.add_argument('--jobs',type=int,default=2)
    settings.add_config_arg(parser)
    args = parser.parse_args()
    settings.load(args.config)
    if not 1<=args.jobs<=4:
        parser.error('--jobs must be 1..4')
    layout = json.loads(Path(args.layout).read_text(encoding='utf-8'))
    inputs = [args.edl,args.layout]+list(layout['sources'].values())+[layout[k] for k in ['transcript','sync','cut_points']]
    out = output_path(args.output,inputs)
    work = output_path(Path(args.work_dir)/'render-plan.json',inputs).parent
    edl,transcript,sync,points = [json.loads(Path(p).read_text(encoding='utf-8'))
                                  for p in [args.edl,layout['transcript'],layout['sync'],layout['cut_points']]]
    plan = make_plan(edl,layout,transcript,sync,points,args.preview)
    enc = encoder(args.preview)
    write_json(work/'render-plan.json',dict(plan,encoder=enc))
    assets = pip_assets(work,layout,plan)
    files = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(render_piece,p,layout,plan,enc,work,assets):p for p in plan['pieces']}
        for future in as_completed(futures):
            piece = futures[future]
            files[piece['index']] = future.result()
            print(f'VIDEO {len(files)}/{len(plan["pieces"])} piece={piece["index"]} mode={piece["mode"]} transition={bool(piece["transition"])}',flush=True)
    concat = work/'video-concat.txt'
    concat_file([files[i] for i in sorted(files)],concat,[p['frames']/plan['fps'] for p in plan['pieces']])
    video = work/'joined-video.mp4'
    run(['-loglevel','error','-f','concat','-safe','0','-i',concat,'-c','copy',video],work/'video-concat.log')
    crossfade = layout.get('audio',{}).get('crossfade_s',settings.load()['audio']['crossfade_s'])
    edited = edit_audio(layout['sources']['webcam'],edl,work,crossfade)
    normalized,loudness = normalize_audio(edited,work)
    run(['-loglevel','error','-i',video,'-i',normalized,'-map','0:v:0','-map','1:a:0',
         '-c:v','copy','-c:a','aac','-ar','48000','-ac','2','-b:a','192k',
         '-t',f'{plan["duration"]:.9f}','-movflags','+faststart',out],work/'mux.log')
    metadata = probe(out)
    stream = next(s for s in metadata['streams'] if s['codec_type']=='video')
    if stream['width']!=plan['width'] or stream['height']!=plan['height'] or stream['r_frame_rate']!='30/1':
        raise ValueError('Final video format differs from requested dimensions/fps')
    if abs(float(metadata['format']['duration'])-edl['output_duration_s'])>.1:
        raise ValueError('Final video duration differs from EDL by more than 0.1s')
    write_json(work/'render-result.json',{'output':str(out),'metadata':metadata,'audio':loudness,
                                        'duration_difference_s':float(metadata['format']['duration'])-edl['output_duration_s']})
    print(f'COMPLETE {out}',flush=True)


if __name__=='__main__':
    main()
