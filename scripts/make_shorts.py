"""Vertical Shorts from raw sources + an approved EDL. Offline, config-driven, cached."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct

import numpy as np
import soundfile as sf
from scipy import signal

from render_common import run, probe, encoder, video_options, concat_file, json_measure
from render_base import offset_at, sync_steps, edit_audio, normalize_audio
from mix_music import duck_envelope, loudness
from sync import residual_lag
from timemap import TimeMap, source_words, correct_text
import settings

FPS = 30


def colors(cfg=None):
    """Global settings colours, overridable per project with a \"colors\" block in shorts.json."""
    return dict(settings.load()['colors'], **((cfg or {}).get('colors') or SHORTS_COLORS))


SHORTS_COLORS = {}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def asset(path):
    p = Path(path)
    st = p.stat()
    return [str(p.resolve()), st.st_size, st.st_mtime_ns]


def ass_time(seconds):
    cs = round(seconds*100)
    return f'{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}'


def stamp(seconds, srt=False):
    ms = round(seconds*1000)
    if srt:
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    return f'{ms//60000:02}:{(ms%60000)/1000:05.2f}'


def filter_path(path):
    return str(Path(path).resolve()).replace('\\', '/').replace(':', r'\:').replace("'", r"\'")


def boundaries_safe(keep, words):
    violations = []
    for k in keep:
        for t in (k['start'], k['end']):
            for w in words:
                if w['start']+1e-6 < t < w['end']-1e-6:
                    violations.append({'boundary':t,'word':w})
    return violations


def make_plan(short, edl, all_words, sync, fps):
    keep = []
    for ri, r in enumerate(short['ranges']):
        if r['mode'] not in ('TALK-V', 'SPLIT') or not 0 <= r['start'] < r['end'] <= edl['raw_duration_s']:
            raise ValueError(f'Invalid range: {r}')
        for k in edl['keep']:
            a, b = max(r['start'], k['start']), min(r['end'], k['end'])
            if b <= a:
                continue
            spans = [(a,b)]
            for cut in short.get('exclude', []):
                next_spans = []
                for x,y in spans:
                    if cut['end'] <= x or cut['start'] >= y:
                        next_spans.append((x,y))
                    else:
                        if x < cut['start']: next_spans.append((x,cut['start']))
                        if cut['end'] < y: next_spans.append((cut['end'],y))
                spans = next_spans
            for x,y in spans:
                # Move an approximate outer boundary OUT of a word, never trim a syllable.
                for w in all_words:
                    if w['start']+1e-6 < x < w['end']-1e-6: x = w['end']
                    if w['start']+1e-6 < y < w['end']-1e-6: y = w['start']
                if y-x >= .04:
                    keep.append({'start':x,'end':y,'range_index':ri})
    keep.sort(key=lambda k:k['start'])
    violations = boundaries_safe(keep, all_words)
    if violations: raise ValueError(f'Word boundary violations: {violations[:3]}')
    mapping = TimeMap(keep, edl['raw_duration_s'])
    if not 35 <= mapping.duration <= 60:
        raise ValueError(f"{short['id']}: duration {mapping.duration:.3f} outside 35–60s; edit JSON exclusions")
    pieces = []
    sync_bounds,_ = sync_steps(sync)
    for ki,k in enumerate(mapping.keep):
        r = short['ranges'][k['range_index']]
        breaks = [k['start'],k['end']]
        if r['mode'] == 'SPLIT':
            breaks += [t for t in sync_bounds if k['start'] < t < k['end']]
            for region in r.get('regions', []):
                breaks += [t for t in (region['start'],region['end']) if k['start'] < t < k['end']]
        breaks = sorted(set(breaks))
        for a,b in zip(breaks,breaks[1:]):
            out_a = k['output_start']+a-k['start']
            out_b = k['output_start']+b-k['start']
            fa,fb = round(out_a*fps),round(out_b*fps)
            if fb <= fa: continue
            rect = r.get('region')
            for region in r.get('regions',[]):
                if region['start'] <= (a+b)/2 < region['end']: rect = region['rect']
            pieces.append({'start':a,'end':b,'out_start':fa/fps,'frames':fb-fa,'mode':r['mode'],
                           'rect':rect,'mask_below':r.get('mask_below'),
                           'keep_index':ki,'keep_start':k['start'],'keep_end':k['end'],
                           'offset':offset_at((a+b)/2,sync)})
    assert sum(p['frames'] for p in pieces)==round(mapping.duration*fps)
    words = []
    for k in mapping.keep:
        for w in all_words:
            if w['end'] > w['start'] and w['start'] >= k['start']-1e-6 and w['end'] <= k['end']+1e-6:
                words.append({'start':k['output_start']+w['start']-k['start'],
                              'end':k['output_start']+w['end']-k['start'],
                              'webcam_start':w['start'],'webcam_end':w['end'],'word':w['word'].strip(),
                              'mode':short['ranges'][k['range_index']]['mode']})
    if not words or words[0]['start'] > .15:
        raise ValueError('The first spoken word must start within 150ms')
    return {'id':short['id'],'keep':mapping.keep,'duration':round(mapping.duration*fps)/fps,
            'exact_duration':mapping.duration,'pieces':pieces,'words':words,
            'word_boundary_violations':violations,'excluded':short.get('exclude',[])}


def correct_words(words, config):
    result = []
    for i,w in enumerate(words):
        w = dict(w)
        original = w['word']
        # A word that opens a protected phrase (e.g. a title like "Cloud City") skips the shared rules.
        pair = ' '.join(x['word'].strip() for x in words[i:i+2]).lower()
        if any(pair.startswith(p.lower()) for p in config.get('protect_phrases',[])):
            text = original
        else:
            text = correct_text(original, -1, config.get('asr_rules',[]))
        for rule in config.get('scoped_corrections',[]):
            if rule['start']<=w['webcam_start']<rule['end']:
                text=re.sub(r'\b'+re.escape(rule['from'])+r'\b',rule['to'],text,flags=re.I)
        w['word'] = config.get('corrections',{}).get(text, text)
        result.append(w)
    joined = []
    for w in result:
        if joined and (w['word'].startswith(("'", "’")) or re.fullmatch(r'\.\d+',w['word'])):
            joined[-1]['word'] += w['word']
            joined[-1]['end'] = w['end']
            joined[-1]['webcam_end'] = w['webcam_end']
        else:
            joined.append(w)
    result = joined
    for before,after in config.get('phrase_corrections',[]):
        terms = before.split()
        i = 0
        while i <= len(result)-len(terms):
            selected = result[i:i+len(terms)]
            if [w['word'].strip('.,!?') for w in selected] == terms:
                merged = dict(selected[0])
                merged.update(word=after,end=selected[-1]['end'],webcam_end=selected[-1]['webcam_end'])
                result[i:i+len(terms)] = [merged]
            i += 1
    return result


class FontWidth:
    """Read installed TrueType metrics; no additional font package required."""
    def __init__(self, path):
        data = Path(path).read_bytes()
        count = struct.unpack_from('>H',data,4)[0]
        tables = {}
        for i in range(count):
            tag,_,offset,length = struct.unpack_from('>4sIII',data,12+i*16)
            tables[tag.decode()] = (offset,length)
        head = tables['head'][0]
        self.units = struct.unpack_from('>H',data,head+18)[0]
        nmetrics = struct.unpack_from('>H',data,tables['hhea'][0]+34)[0]
        self.widths = [struct.unpack_from('>H',data,tables['hmtx'][0]+i*4)[0] for i in range(nmetrics)]
        cmap = tables['cmap'][0]
        n = struct.unpack_from('>H',data,cmap+2)[0]
        self.glyphs = {}
        for i in range(n):
            platform,encoding,offset = struct.unpack_from('>HHI',data,cmap+4+i*8)
            pos = cmap+offset
            fmt = struct.unpack_from('>H',data,pos)[0]
            if fmt != 4: continue
            segs = struct.unpack_from('>H',data,pos+6)[0]//2
            endpos = pos+14
            startpos = endpos+segs*2+2
            deltapos = startpos+segs*2
            rangepos = deltapos+segs*2
            for j in range(segs):
                end,start,delta,off = (struct.unpack_from('>H',data,q+j*2)[0]
                                      for q in (endpos,startpos,deltapos,rangepos))
                for c in range(start,min(end+1,65535)):
                    if not off: gid=(c+delta)%65536
                    else:
                        gid=struct.unpack_from('>H',data,rangepos+j*2+off+2*(c-start))[0]
                        if gid: gid=(gid+delta)%65536
                    self.glyphs[c]=gid
            break
        if not self.glyphs: raise ValueError('TrueType Unicode cmap format 4 not found')

    def width(self,text,size):
        # Synthetic bold + libass border are included conservatively.
        return sum(self.widths[min(self.glyphs.get(ord(c),0),len(self.widths)-1)] for c in text)/self.units*size*1.07+20


def captions(plan, config, work, dest, qa):
    words = correct_words(plan['words'], config)
    font = config['font']
    # shortcut: local font is 600; supply a genuine 700 file in JSON when available.
    metrics = FontWidth(font['file'])
    groups = []
    group = []
    for w in words:
        text = ' '.join(x['word'] for x in group+[w])
        if group and (sum(len(x['word'].split()) for x in group+[w])>3 or group[-1]['mode']!=w['mode']
                      or w['start']-group[-1]['end']>.3
                      or group[-1]['word'].endswith(('.', '?', '!'))
                      or metrics.width(text,font['size'])>font['max_width']):
            groups.append(group); group=[]
        group.append(w)
    if group: groups.append(group)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font['family']},{font['size']},&H00FFFFFF,&H00{settings.ass_bgr(colors()['accent'])},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{font['outline']},{font['shadow']},5,60,140,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events=[]; srt=[]; report=[]; max_width=0
    for j,g in enumerate(groups):
        text=' '.join(w['word'] for w in g)
        width=metrics.width(text,font['size'])
        if width > font['max_width']:
            raise ValueError(f'Caption wider than safe area: {text} ({width})')
        max_width=max(max_width,width)
        srt.append(f"{j+1}\n{stamp(g[0]['start'],True)} --> {stamp(g[-1]['end'],True)}\n{text}\n")
        report.append(f"{stamp(g[0]['start'])}–{stamp(g[-1]['end'])} | {text}")
        for wi,w in enumerate(g):
            a=w['start']; b=g[wi+1]['start'] if wi+1<len(g) else w['end']
            if b <= a: continue
            y=1037 if w['mode']=='SPLIT' else (1090 if a>=plan['duration']-2.3 else 1306)
            accent=settings.ass_bgr(colors()['accent'])
            highlighted=' '.join(('{\\c&H'+accent+'&}'+x['word']+'{\\c&HFFFFFF&}') if q==wi else x['word']
                                 for q,x in enumerate(g))
            events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,"
                          f"{{\\an5\\pos(500,{y})\\b700}}{highlighted}")
    ass=work/'captions.ass'
    ass.write_text(header+'\n'.join(events)+'\n',encoding='utf-8-sig')
    dest.with_suffix('.srt').write_text('\n'.join(srt),encoding='utf-8')
    (qa/f"{plan['id']}-captions.txt").write_text('\n'.join(report)+'\n',encoding='utf-8')
    write(work/'out_words.json',{'words':words,'caption_max_width_px':max_width,
                                'safe_area':[60,220,940,1540]})
    return ass, words


def face_filter(rect, width, height, piece, fps, track=None):
    x,y,w,h = rect
    base = 1.1 if piece['keep_index']%2 else 1.
    duration=max(.1,piece['keep_end']-piece['keep_start'])
    progress=(piece['start']-piece['keep_start'])/duration
    zoom=f'{base+progress*.02:.9f}+0.02*on/{duration*fps:.6f}'
    # Oversample before zoompan; keeping a high hairline anchor prevents a zoom from cutting hair.
    crop_x = str(x)
    if track:
        points = sorted(track,key=lambda p:p['time'])
        selected = [p for p in points if piece['start']<=p['time']<=piece['end']]
        before = [p for p in points if p['time']<piece['start']]
        after = [p for p in points if p['time']>piece['end']]
        selected = before[-1:]+selected+after[:1]
        expr = str(selected[-1]['x'])
        for a,b in reversed(list(zip(selected,selected[1:]))):
            local_a,local_b=a['time']-piece['start'],b['time']-piece['start']
            interp=f"{a['x']}+({b['x']-a['x']})*(t-({local_a:.6f}))/{local_b-local_a:.6f}"
            expr=f"if(lt(t,{local_b:.6f}),{interp},{expr})"
        crop_x = f"'max(0,min(iw-{w},({expr})-{w/2}))'"
    padding = f'pad={max(1920,x+w)}:{max(1080,y+h)}:0:0:color=black,' if y+h>1080 or x+w>1920 else ''
    return (padding+f'crop={w}:{h}:{crop_x}:{y},scale={width*2}:{height*2}:flags=lanczos,'
            f"zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='(ih-ih/zoom)*0.14':"
            f'd=1:s={width}x{height}:fps={fps},setsar=1')


def render_piece(piece, short, cfg, ass, work, enc):
    index=piece['index']
    out=work/f'piece-{index:03}.mp4'
    cache=out.with_suffix('.json')
    import inspect
    version=signature([inspect.getsource(face_filter), inspect.getsource(render_piece)])
    key=signature([piece,short['talk_crop'] if piece['mode']=='TALK-V' else cfg['split_face'],
                   short.get('talk_track',[]) if piece['mode']=='TALK-V' else [],asset(cfg['sources']['webcam']),
                   asset(cfg['sources']['screen']),hashlib.sha256(ass.read_bytes()).hexdigest(),asset(cfg['font']['file']),version])
    if out.exists() and cache.exists() and read(cache).get('signature')==key: return out
    args=['-loglevel','warning','-filter_complex_threads','2',
          '-ss',f"{piece['start']:.9f}",'-threads','2','-i',cfg['sources']['webcam']]
    if piece['mode']=='SPLIT':
        screen_start=piece['start']-piece['offset']
        if screen_start<0: raise ValueError('Screen not yet recording')
        args+=['-ss',f'{screen_start:.9f}','-threads','2','-i',cfg['sources']['screen']]
    n=piece['frames']; fps=cfg['fps']
    prefix=f'fps={fps},trim=end_frame={n},setpts=PTS-STARTPTS,'
    if piece['mode']=='TALK-V':
        graph=f"[0:v]{prefix}"+face_filter(short['talk_crop'],1080,1920,piece,fps,short.get('talk_track'))+'[layout];'
    else:
        face=face_filter(cfg['split_face'],1080,820,piece,fps)
        x,y,w,h=piece['rect']
        if x<0 or y<0 or w<=0 or h<=0: raise ValueError('Invalid screen crop')
        canvas_w=max(1920,x+w); canvas_h=max(1080,y+h)
        masking=''
        if piece['mask_below']:
            masking=f"drawbox=x=0:y={piece['mask_below']}:w=iw:h=ih:color={colors()['screen_fill']}:t=fill,"
        graph=(f'[0:v]{prefix}{face}[face];[1:v]{prefix}{masking}'
               f'pad={canvas_w}:{canvas_h}:0:0:color={colors()["screen_fill"]},crop={w}:{h}:{x}:{y},'
               'scale=1080:1100:flags=lanczos,setsar=1[screen];'
               f'[screen][face]vstack=inputs=2,drawbox=x=0:y=1097:w=1080:h=6:color={colors()["divider"]}:t=fill[stack];'
               f"color=c={colors()['accent']}:s=16x16:r=30,format=rgba,"
               "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lte(hypot(X-7.5,Y-7.5),7.5),255,0)'[dot];"
               '[stack][dot]overlay=x=532:y=1092:shortest=1[layout];')
    graph+=(f"[layout]setpts=PTS+{piece['out_start']:.9f}/TB,"
            f"ass=filename='{filter_path(ass)}':fontsdir='{filter_path(work.parent/'fonts')}',"
            'setpts=PTS-STARTPTS,format=yuv420p[out]')
    graph_file=work/f'piece-{index:03}.ffscript'
    graph_file.write_text(graph,encoding='utf-8')
    run(args+['-filter_complex_script',graph_file,'-map','[out]','-an','-frames:v',n]
        +video_options(enc,fps)+[out],work/f'piece-{index:03}.log')
    write(cache,{'signature':key})
    return out


def build_audio(plan,cfg,work):
    edited=edit_audio(cfg['sources']['webcam'],
                      {'keep':plan['keep'],'raw_duration_s':read(cfg['sources']['edl'])['raw_duration_s']},work,
                      settings.load()['audio']['crossfade_s'])
    voice,normalization=normalize_audio(edited,work)
    voice_data,sr=sf.read(voice,always_2d=True,dtype='float32')
    count=round(plan['duration']*sr)
    voice_data=np.pad(voice_data,((0,max(0,count-len(voice_data))),(0,0)))[:count]
    music_file=work/'music-excerpt.wav'
    run(['-loglevel','error','-ss',cfg['music']['body_start'],'-i',cfg['music']['file'],
         '-t',plan['duration'],'-ar',sr,'-ac',2,'-c:a','pcm_f32le',music_file])
    bed,_=sf.read(music_file,always_2d=True,dtype='float32')
    if len(bed)<count: raise ValueError('Music body is shorter than this Short; choose an earlier body_start')
    bed=bed[:count]
    time,reduction=duck_envelope(voice_data.mean(axis=1),sr,plan['duration'],0,0,0,
                                 maximum=cfg['music']['duck_max_db'])
    gains=np.interp(np.arange(count)/sr,time,10**(-reduction/20))
    bed*=gains[:,None]
    fade=min(count,round(cfg['music']['fade_out']*sr))
    bed[-fade:]*=np.linspace(1,0,fade)[:,None]
    fade_in=min(count,round(.08*sr))
    bed[:fade_in]*=np.linspace(0,1,fade_in)[:,None]
    sf.write(music_file,bed,sr,subtype='FLOAT')
    stats=loudness(music_file)
    bed*=10**((cfg['music']['lufs']-float(stats['input_i']))/20)
    sf.write(music_file,bed,sr,subtype='FLOAT')
    # Preserve the calibrated speech; final mastering below only applies a common small gain/peak guard.
    voice_data[:240]*=np.linspace(0,1,240)[:,None]
    voice_data[-240:]*=np.linspace(1,0,240)[:,None]
    sf.write(work/'voice-music.wav',voice_data+bed,sr,subtype='FLOAT')
    write(work/'audio.json',{'speech':normalization,'music_lufs':loudness(music_file)['input_i'],
                              'duck_max_db':float(reduction.max()),'crossfade_ms':12})
    return work/'voice-music.wav'


def load_extra_overlays(path, short_ids):
    if not path:
        return {}
    if not Path(path).is_file():
        print(f'WARNING missing extra overlay JSON, skipped: {path}',flush=True)
        return {}
    data=read(path)
    if not isinstance(data,dict):
        raise ValueError('Extra overlays JSON must be an object keyed by Short id')
    result={}
    for ident,entry in data.items():
        if ident not in short_ids:
            print(f'WARNING unknown Short id, skipped: {ident}',flush=True)
            continue
        if not isinstance(entry,dict):
            raise ValueError(f'{ident}: expected overlays/sfx object')
        result[ident]={}
        for kind in ('overlays','sfx'):
            tracks=entry.get(kind,[])
            if not isinstance(tracks,list):
                raise ValueError(f'{ident}.{kind}: expected a list')
            normalized=[]
            for track in tracks:
                if not isinstance(track,dict) or not isinstance(track.get('file'),str) or not track['file']:
                    raise ValueError(f'{ident}.{kind}: each entry requires a file')
                start=float(track['start'])
                gain=float(track.get('gain_db',-20))
                if not math.isfinite(start) or start<0 or not math.isfinite(gain):
                    raise ValueError(f'{ident}.{kind}: start must be nonnegative and all numbers finite')
                normalized.append({**track,'start':start,**({'gain_db':gain} if kind=='sfx' else {})})
            result[ident][kind]=normalized
    return result


def short_output(short, outdir, suffix, original_dir, extra=False, prefix='short'):
    if not re.fullmatch(r'[a-zA-Z0-9_-]*',suffix):
        raise ValueError('Output suffix must use letters, digits, hyphens or underscores')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+',prefix):
        raise ValueError('name_prefix must use letters, digits, hyphens or underscores')
    filename=f"{prefix}-{short['n']:02}-{short['id']}"
    dest=Path(outdir)/(filename+suffix+'.mp4')
    if extra and dest.resolve()==(Path(original_dir)/(filename+'.mp4')).resolve():
        raise ValueError('Extra overlays cannot overwrite v1; set --output-suffix -v2 or --output-dir')
    return dest


def composite(plan,short,cfg,work,dest,enc,extra=None):
    base=work/'base.mp4'
    if not base.exists() or not (work/'voice-music.wav').exists():
        raise ValueError('--overlays-only requires cached base.mp4 and voice-music.wav')
    extra=extra or {}
    overlays=[]; missing=[]
    tracks=[{**o,'kind':'v2'} for o in extra.get('overlays',[])]
    tracks += [{'kind':kind,'file':cfg['overlays'][kind].format(n=short['n']),'start':start}
               for kind,start in [('hook',0.),('cta',plan['duration']-cfg['overlays']['cta_duration'])]]
    for track in tracks:
        name=track['file']; start=track['start']
        if not math.isfinite(start) or start<0:
            raise ValueError(f'Invalid overlay start: {track}')
        if not Path(name).is_file():
            missing.append(name); print(f'WARNING missing overlay, skipped: {name}',flush=True); continue
        if start>=plan['duration']:
            print(f'WARNING overlay outside Short, skipped: {name} at {start}',flush=True); continue
        stream=next(s for s in probe(name)['streams'] if s['codec_type']=='video')
        if (stream['width'],stream['height']) != (1080,1920):
            raise ValueError(f'Overlay dimensions must be 1080x1920: {name}')
        overlays.append(track)
    extra_visuals=any(o['kind']=='v2' for o in overlays)
    ass=work/'captions.ass'
    if extra_visuals and not ass.is_file():
        raise ValueError('V2 compositing requires cached captions.ass')
    samples,sr=sf.read(work/'voice-music.wav',always_2d=True,dtype='float32')
    count=round(plan['duration']*sr)
    if len(samples)<count:
        raise ValueError('Cached voice/music is shorter than the Short')
    samples=samples[:count].copy()
    sfx_added=[]
    # Audible hook/CTA accents also exist when their graphic is still being produced.
    sounds=[{'kind':kind,'start':start,**cfg['sfx'][kind]}
            for kind,start in [('hook',0.),('cta',plan['duration']-cfg['overlays']['cta_duration'])]]
    sounds += [{**s,'kind':'v2'} for s in extra.get('sfx',[])]
    for i,sfx in enumerate(sounds):
        start=sfx['start']; gain_db=sfx.get('gain_db',-20)
        if not math.isfinite(start) or start<0 or not math.isfinite(gain_db):
            raise ValueError(f'Invalid SFX timing/gain: {sfx}')
        if not Path(sfx['file']).is_file():
            print(f"WARNING missing SFX, skipped: {sfx['file']}",flush=True); continue
        if start>=plan['duration']:
            print(f"WARNING SFX outside Short, skipped: {sfx['file']} at {start}",flush=True); continue
        wav=work/(f"sfx-v2-{i:03}.wav" if sfx['kind']=='v2' else f"sfx-{sfx['kind']}.wav")
        run(['-loglevel','error','-i',sfx['file'],'-ar',sr,'-ac',2,'-c:a','pcm_f32le',wav])
        effect,_=sf.read(wav,always_2d=True,dtype='float32')
        first=round(start*sr); size=min(len(effect),len(samples)-first)
        samples[first:first+size]+=effect[:size]*10**(gain_db/20)
        sfx_added.append(sfx)
    samples[-480:]*=np.linspace(1,0,480)[:,None]
    sf.write(work/'mix.wav',samples,sr,subtype='FLOAT')
    master=work/'master.wav'
    loud=settings.loudnorm(.6)
    audio_cfg=settings.load()['audio']
    stats=json_measure(run(['-i',work/'mix.wav','-af',loud+':print_format=json','-f','null','-']))
    measured=(f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
              f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
              f":offset={stats['target_offset']}:linear=true")
    run(['-loglevel','error','-i',work/'mix.wav','-af',loud+measured,
         '-ar',48000,'-c:a','pcm_s24le',master])
    audio=work/'master.m4a'
    run(['-loglevel','error','-i',master,'-c:a','aac','-b:a','192k','-ar',48000,audio])
    measure=loudness(audio)
    gain=min(audio_cfg['target_lufs']-float(measure['input_i']),audio_cfg['true_peak_dbtp']-.15-float(measure['input_tp']))
    if abs(gain)>.08:
        run(['-loglevel','error','-i',master,'-af',f'volume={gain:.6f}dB',
             '-c:a','aac','-b:a','192k','-ar',48000,audio])
        measure=loudness(audio)
    if not settings.loudness_ok(measure):
        raise ValueError(f'Audio master outside target: {measure}')
    args=['-loglevel','warning','-filter_complex_threads','2','-i',base,'-i',audio]
    graph=''; previous='0:v'; captions_applied=False
    for i,o in enumerate(overlays):
        if extra_visuals and o['kind']!='v2' and not captions_applied:
            # shortcut: legacy caches have burned captions; use a clean cache if later caption text/positions must change.
            graph+=(f"[{previous}]ass=filename='{filter_path(ass)}':"
                    f"fontsdir='{filter_path(work.parent/'fonts')}'[captions];")
            previous='captions'; captions_applied=True
        args+=['-threads','2','-i',o['file']]
        graph+=f"[{i+2}:v]setpts=PTS-STARTPTS+{o['start']:.9f}/TB[ov{i}];"
        graph+=(f"[{previous}][ov{i}]overlay=eof_action=pass:repeatlast=0:shortest=0:"
                f"format=auto:enable='gte(t,{o['start']:.9f})'[v{i}];")
        previous=f'v{i}'
    if extra_visuals and not captions_applied:
        graph+=(f"[{previous}]ass=filename='{filter_path(ass)}':"
                f"fontsdir='{filter_path(work.parent/'fonts')}'[captions];")
        previous='captions'
    if overlays:
        graph_file=work/'composite.ffscript'
        graph_file.write_text(graph.rstrip(';'),encoding='utf-8')
        run(args+['-filter_complex_script',graph_file,'-map',f'[{previous}]','-map','1:a']
            +video_options(enc,cfg['fps'])+['-c:a','copy','-t',plan['duration'],
                                           '-movflags','+faststart',dest],work/'composite.log')
    else:
        run(args+['-map','0:v','-map','1:a','-c','copy','-t',plan['duration'],
                  '-movflags','+faststart',dest],work/'composite.log')
    return {'overlays':overlays,'missing_overlays':missing,'sfx':sfx_added,'loudness':measure,
            'layer_order':['video','v2','captions','hook/cta'],
            'master_gain_db':gain,'file':str(dest),'duration':plan['duration']}


def contact_sheet(plan,dest,work,qa,preferred=None,font_file=None):
    changes=[p['out_start'] for a,p in zip(plan['pieces'],plan['pieces'][1:]) if p['mode']!=a['mode']]
    m=TimeMap(plan['keep'],max(k['end'] for k in plan['keep']))
    preferred=[m.to_output(t) for t in (preferred or [])]
    times=sorted(set([.18,1.4]+changes+[plan['duration']-1]+preferred))
    if len(times)>8: raise ValueError('More than eight mandatory contact frames')
    while len(times)<8:
        a,b=max(zip(times,times[1:]),key=lambda pair:pair[1]-pair[0])
        times.append((a+b)/2); times.sort()
    frames=[]
    for i,t in enumerate(times):
        frame=work/f'contact-{i}.png'
        # A labeled strip below each image leaves its full safe area visible.
        text=f"{plan['id']}  {stamp(t)}"
        label=work/f'label-{i}.txt';label.write_text(text,encoding='utf-8')
        run(['-loglevel','error','-ss',t,'-i',dest,'-frames:v',1,
             '-vf',f"scale=270:480,pad=270:512:0:0:color={colors()['contact_sheet']},"
             f"drawtext=fontfile='{filter_path(font_file or next((work.parent/'fonts').glob('*.ttf')))}':"
             f"textfile='{filter_path(label)}':fontcolor=white:fontsize=13:x=6:y=488",frame])
        frames.append(frame)
    args=['-loglevel','error','-filter_complex_threads','2']
    for f in frames: args+=['-i',f]
    graph=''.join(f'[{i}:v]' for i in range(len(frames)))+f'xstack=inputs={len(frames)}:layout='
    graph+='|'.join(f'{i%4*270}_{i//4*512}' for i in range(len(frames)))+'[out]'
    output=qa/f"{plan['id']}-contact.jpg"
    run(args+['-filter_complex',graph,'-map','[out]','-frames:v',1,'-q:v',2,output])
    return str(output),times


def qa_short(plan,cfg,dest,work,qa):
    info=probe(dest); video=next(s for s in info['streams'] if s['codec_type']=='video')
    duration=float(info['format']['duration'])
    audio=next(s for s in info['streams'] if s['codec_type']=='audio')
    assert video['codec_name']=='h264' and audio['codec_name']=='aac'
    assert audio['sample_rate']=='48000' and audio['channels']==2
    assert int(video['nb_frames'])==round(plan['duration']*cfg['fps'])
    assert (video['width'],video['height'],video['r_frame_rate'])==(1080,1920,'30/1')
    assert duration<=60.001 and abs(duration-plan['duration'])<.05
    log=run(['-v','error','-i',dest,'-f','null','-'],work/'decode.log')
    if log.strip(): raise ValueError(f'Decode errors: {log}')
    loud=loudness(dest)
    assert settings.loudness_ok(loud),loud
    selected=next(s for s in cfg['shorts'] if s['id']==plan['id'])
    contact,times=contact_sheet(plan,dest,work,qa,selected.get('qa_webcam_times'),cfg['font']['file'])
    sync_result=None
    # Use a stable interior point: no jump cuts, no sync plateau boundary.
    split=sorted((p for p in plan['pieces'] if p['mode']=='SPLIT' and p['frames']/cfg['fps']>3),
                 key=lambda p:p['frames'],reverse=True)
    if split:
        p=split[0]; start=p['out_start']+.5; webcam=p['start']+.5
        wavs=[]
        for file,t,name in [(dest,start,'final'),(cfg['sources']['screen'],webcam-p['offset'],'screen')]:
            wav=work/f'sync-{name}.wav'
            run(['-loglevel','error','-ss',t,'-i',file,'-t',2.,'-vn','-ac',1,'-ar',16000,
                 '-c:a','pcm_f32le',wav])
            arr,sr=sf.read(wav,dtype='float32')
            sos=signal.butter(3,[180,3200],btype='bandpass',fs=sr,output='sos')
            wavs.append(signal.sosfilt(sos,arr))
        lag,confidence=residual_lag(wavs[0],wavs[1],16000,.15)
        sync_result={'output_time':start,'webcam_time':webcam,'screen_time':webcam-p['offset'],
                     'residual_ms':lag*1000,'correlation':confidence}
        assert abs(lag)<=.05, sync_result
    return {'duration':duration,'width':video['width'],'height':video['height'],'fps':video['r_frame_rate'],
            'decode':'OK','loudness':loud,'sync':sync_result,'contact':contact,'contact_times':times,
            'word_boundary_violations':0,'first_word':plan['words'][0],
            'last_word':plan['words'][-1]}


def self_check():
    words=[{'start':1.,'end':1.4,'word':'Merhaba'},{'start':1.5,'end':1.8,'word':'dünya'}]
    assert len(boundaries_safe([{'start':1.1,'end':1.8}],words))==1
    assert not boundaries_safe([{'start':.9,'end':1.8}],words)
    mapping=TimeMap([{'start':1.,'end':2.},{'start':4.,'end':5.}],6.)
    assert mapping.to_output(3.)==1. and mapping.to_webcam(1.)==4.
    assert ass_time(65.12)=='0:01:05.12' and stamp(65.12,True)=='00:01:05,120'
    test=correct_words([{'start':0,'end':1,'webcam_start':100,'webcam_end':101,'word':'Cloud'},
                        {'start':1,'end':2,'webcam_start':101,'webcam_end':102,'word':'City'}],{})
    assert test[0]['word']=='Cloud'
    title_rules={'asr_rules':[{'pattern':r'\bCloud\b','replacement':'Claude'}],'protect_phrases':['Cloud City']}
    test=correct_words([{'start':0,'end':1,'webcam_start':100,'webcam_end':101,'word':'Cloud'},
                        {'start':1,'end':2,'webcam_start':101,'webcam_end':102,'word':'City'},
                        {'start':2,'end':3,'webcam_start':102,'webcam_end':103,'word':'Cloud'}],title_rules)
    assert [w['word'] for w in test]==['Cloud','City','Claude']
    demo={'id':'test','ranges':[{'start':10.,'end':60.,'mode':'TALK-V'}],
          'exclude':[{'start':40.,'end':45.}]}
    edl={'raw_duration_s':100.,'keep':[{'start':10.,'end':25.},{'start':30.,'end':60.}]}
    p=make_plan(demo,edl,[{'start':10.,'end':11.,'word':'Merhaba'}],{'screen_offset_s':0.},30)
    assert p['duration']==40. and len(p['keep'])==3 and not p['word_boundary_violations']
    sample={'word':'hisse','webcam_start':75.,'webcam_end':76.,'start':0.,'end':1.}
    rules={'scoped_corrections':[{'start':73,'end':78,'from':'hisse','to':'iş'}]}
    assert correct_words([sample],rules)[0]['word']=='iş'
    sample['webcam_start']=80.
    assert correct_words([sample],rules)[0]['word']=='hisse'
    opts=parse_args(['--output-suffix','-v2','--output-dir','work/shorts-test','--overlays-only'])
    assert opts.output_suffix=='-v2' and opts.overlays_only
    short={'n':1,'id':'sample'}
    assert short_output(short,'work/shorts-test','-v2','renders/shorts',True).name=='short-01-sample-v2.mp4'
    assert short_output(short,'work/shorts-test','','renders/shorts',prefix='demo').name=='demo-01-sample.mp4'
    try:
        short_output(short,'renders/shorts','','renders/shorts',True)
    except ValueError:
        pass
    else:
        raise AssertionError('v1 overwrite must be rejected')
    print('self-check OK',flush=True)


def parse_args(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='edit/shorts.json',type=Path)
    parser.add_argument('--only',help='Comma-separated IDs or numbers')
    parser.add_argument('--extra-overlays',type=Path,help='Short-id keyed v2 overlays/SFX JSON (paths relative to project root)')
    parser.add_argument('--output-suffix',default='',help='Output filename suffix, e.g. -v2')
    parser.add_argument('--output-dir',type=Path,help='Override the output video/SRT directory')
    parser.add_argument('--overlays-only',action='store_true',help='Recompose cached captioned base + cached music/voice; skip heavy rendering')
    parser.add_argument('--qa-only',action='store_true')
    parser.add_argument('--plan-only',action='store_true')
    parser.add_argument('--self-check',action='store_true')
    parser.add_argument('--settings',help='Pipeline settings JSON (default: $KURGU_CONFIG or ./kurgu.config.json)')
    import sys
    argv=list(sys.argv[1:] if argv is None else argv)
    if '--output-suffix' in argv:
        i=argv.index('--output-suffix')
        if i+1<len(argv) and re.fullmatch(r'-[a-zA-Z0-9_-]+',argv[i+1]):
            argv[i:i+2]=['--output-suffix='+argv[i+1]]
    return parser.parse_args(argv)


def main():
    args=parse_args()
    if args.self_check: self_check(); return
    cfg_path=args.config.resolve()
    cfg=read(cfg_path)
    settings.load(args.settings)
    SHORTS_COLORS.update(cfg.get('colors') or {})
    import os
    os.chdir((cfg_path.parent/cfg.get('project_root','..')).resolve())
    fps=cfg['fps']
    if fps!=30: raise ValueError('This pipeline expects 30fps per the approved vertical design')
    original_dir=Path(cfg['output_dir'])
    outdir=args.output_dir or original_dir
    extra_path=args.extra_overlays or cfg.get('extra_overlays')
    prefix=cfg.get('name_prefix','short')
    for short in cfg['shorts']:
        short_output(short,outdir,args.output_suffix,original_dir,extra=bool(extra_path),prefix=prefix)
    extras=load_extra_overlays(extra_path,{s['id'] for s in cfg['shorts']})
    variant=args.output_suffix or ('-extra' if extra_path else '-'+outdir.name if outdir.resolve()!=original_dir.resolve() else '')
    workroot=Path(cfg['work_dir']); qa=Path(cfg['qa_dir'])
    if variant: qa=qa/variant.lstrip('-')
    for directory in (outdir,workroot,qa,workroot/'fonts'): directory.mkdir(parents=True,exist_ok=True)
    font_copy=workroot/'fonts'/Path(cfg['font']['file']).name
    if not font_copy.exists() or font_copy.read_bytes()!=Path(cfg['font']['file']).read_bytes():
        shutil.copyfile(cfg['font']['file'],font_copy)
    edl=read(cfg['sources']['edl']); sync=read(cfg['sources']['sync'])
    all_words=source_words(read(cfg['sources']['transcript']))
    selected=set(args.only.split(',')) if args.only else None
    enc=encoder()
    summary=[]
    for short in cfg['shorts']:
        if selected and short['id'] not in selected and str(short['n']) not in selected: continue
        if not re.fullmatch(r'[a-z0-9-]+',short['id']) or not isinstance(short['n'],int) or short['n']<1:
            raise ValueError('Short id must use lowercase ASCII letters/digits/hyphens and n must be positive')
        work=workroot/short['id']; work.mkdir(parents=True,exist_ok=True)
        dest=short_output(short,outdir,args.output_suffix,original_dir,extra=bool(extra_path),prefix=prefix)
        render_report=work/f'render{variant}.json'
        plan=make_plan(short,edl,all_words,sync,fps)
        write(work/'plan.json',plan)
        ass,words=captions(plan,cfg,work,dest,qa)
        print(f"{short['id']}: {plan['duration']:.3f}s; {len(plan['keep'])} keeps; "
              f"{len(plan['pieces'])} render pieces; {len(words)} words",flush=True)
        if args.plan_only: summary.append(plan); continue
        state = signature([plan,{k:v for k,v in short.items() if not k.startswith('qa_')},cfg['font'],cfg['music'],cfg['split_face'],
                           hashlib.sha256(ass.read_bytes()).hexdigest(),
                           [asset(cfg['sources'][name]) for name in ('webcam','screen','edl','transcript','sync')]])
        manifest=work/'base-state.json'
        if args.overlays_only and (not manifest.exists() or read(manifest).get('signature')!=state):
            raise ValueError('Cached base does not match this config; run without --overlays-only first')
        result=read(render_report) if render_report.exists() else {}
        if not args.qa_only:
            if not args.overlays_only:
                paths=[]
                for i,piece in enumerate(plan['pieces']):
                    piece['index']=i
                    print(f"  piece {i+1}/{len(plan['pieces'])} {piece['mode']} webcam={piece['start']:.3f}",flush=True)
                    paths.append(render_piece(piece,short,cfg,ass,work,enc))
                listing=work/'concat.txt'
                concat_file(paths,listing,[p['frames']/fps for p in plan['pieces']])
                silent=work/'vertical-captioned.mp4'
                run(['-loglevel','error','-f','concat','-safe','0','-i',listing,
                     '-map','0:v','-c:v','copy',silent])
                audio=build_audio(plan,cfg,work)
                run(['-loglevel','error','-i',silent,'-i',audio,'-map','0:v','-map','1:a',
                     '-c:v','copy','-c:a','aac','-b:a','192k','-ar',48000,'-t',plan['duration'],
                     '-movflags','+faststart',work/'base.mp4'])
                write(manifest,{'signature':state,'duration':plan['duration']})
            result=composite(plan,short,cfg,work,dest,enc,extras.get(short['id']))
            write(render_report,result)
        result['qa']=qa_short(plan,cfg,dest,work,qa)
        result['excluded']=short.get('exclude',[])
        result['endpoint_note']=short.get('endpoint_note')
        write(qa/f"{short['id']}-qa.json",result)
        summary.append(result)
        print(f"  DONE {dest} LUFS={result['qa']['loudness']['input_i']} "
              f"TP={result['qa']['loudness']['input_tp']}",flush=True)
    write(qa/('plans.json' if args.plan_only else 'summary.json'),summary)


if __name__=='__main__':
    main()
