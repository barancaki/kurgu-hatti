"""Offline render planning and short composite integration checks.

Run from the project root after sync/EDL/layout exist (needs raw/webcam.mp4 and edit/*.json).
"""
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

from render_base import make_plan, offset_at, sync_steps, screen_filter
from render_common import concat_file, encoder, probe, run
from composite import overlay_windows, windowed_video


def main():
    root = Path('work/render-tests')
    root.mkdir(parents=True,exist_ok=True)
    layout = json.loads(Path('edit/layout.json').read_text(encoding='utf-8'))
    edl = json.loads(Path('edit/final_edl.json').read_text(encoding='utf-8'))
    tr = json.loads(Path(layout['transcript']).read_text(encoding='utf-8'))
    sync = json.loads(Path(layout['sync']).read_text(encoding='utf-8'))
    points = json.loads(Path(layout['cut_points']).read_text(encoding='utf-8'))
    plan = make_plan(edl,layout,tr,sync,points)
    assert sum(p['frames'] for p in plan['pieces'])==round(edl['output_duration_s']*30)
    assert abs(plan['duration']-edl['output_duration_s'])<1/30
    assert len(plan['transitions'])==len(layout['modes'])-1 and len(plan['zooms'])<=len(layout.get('zooms',[]))
    starts = {w['start'] for s in tr['segments'] for w in s['words']}
    assert all(m['webcam_start'] in starts for m in plan['modes'][1:])
    boundaries,offsets = sync_steps(sync)
    for boundary,a,b in zip(boundaries,offsets,offsets[1:]):
        assert offset_at(boundary-.001,sync)==a and offset_at(boundary+.001,sync)==b
    assert all(p['screen_start']>=0 for p in plan['pieces'])
    for script in ['render_base','composite','qa_base']:
        result = subprocess.run([sys.executable,f'scripts/{script}.py','--help'],capture_output=True)
        assert result.returncode==0,result.stderr
    concat_test = root/'concat-duration-test.txt'
    concat_file([Path('raw/webcam.mp4')]*3,concat_test,[1/30,2/30,1/30])
    assert concat_test.read_text().count('duration ')==3
    assert 'duration 0.033333333' in concat_test.read_text()
    try:
        concat_file([Path('raw/webcam.mp4')],concat_test,[0])
        raise AssertionError('Invalid concat duration accepted')
    except ValueError:
        pass
    crossing = [{'start':29.5,'mode':'alpha','metadata':{'format':{'duration':'2'}}}]
    windows = list(overlay_windows(crossing,0,61))
    assert windows[0][2][0]['local_start']==29.5 and windows[0][2][0]['seek']==0
    assert windows[1][2][0]['local_start']==0 and windows[1][2][0]['seek']==.5
    assert not windows[2][2] and sum(w[1] for w in windows)==1830
    # Anchored prompt zooms (output_anchor) must build a padded, bounded filter graph.
    zoom = next((z for z in plan['zooms'] if 'output_anchor' in z),None)
    if zoom:
        piece = next(p for p in plan['pieces'] if p['mode']=='SCREEN')
        tiny = dict(piece,output_start=(zoom['output_start']+zoom['output_end'])/2,duration=1/30)
        filt = screen_filter(tiny,layout,plan)
        assert 'pad=3840:2160' in filt and len(filt)<32000
    print(f'PASS plan: global frame count, word-snapped modes, {len(plan["transitions"])} transitions, '
          f'{len(plan["zooms"])} zooms, piecewise offsets, CLI help',flush=True)

    def color_clip(name,color,seconds,hz=None):
        args = ['-loglevel','error','-f','lavfi','-i',f'color=c={color}:s=960x540:r=30:d={seconds}']
        if hz:
            args += ['-f','lavfi','-i',f'sine=frequency={hz}:sample_rate=48000:duration={seconds}']
        args += ['-c:v','libx264','-preset','ultrafast','-crf','24','-pix_fmt','yuv420p']
        if hz:
            args += ['-c:a','aac','-b:a','192k']
        dest = root/f'{name}.mp4'
        run(args+[dest])
        return str(dest)
    base = color_clip('base','gray',.8,500)
    intro = color_clip('intro','green',.3,1000)
    outro = color_clip('outro','yellow',.3,1600)
    mute = color_clip('mute','white',.2)
    alpha = root/'alpha.mov'
    run(['-loglevel','error','-f','lavfi','-i','color=c=red@0.5:s=960x540:r=30:d=0.2,format=rgba',
         '-c:v','prores_ks','-profile:v','4','-pix_fmt','yuva444p10le','-threads','2',alpha])
    full = color_clip('full','blue',.1)
    sound,music = root/'sfx.wav',root/'music.wav'
    run(['-loglevel','error','-f','lavfi','-i','sine=frequency=5000:sample_rate=48000:duration=0.1',sound])
    run(['-loglevel','error','-f','lavfi','-i','sine=frequency=220:sample_rate=48000:duration=0.2',music])
    config = {'prepend':[{'file':intro}],'append':[{'file':outro},{'file':mute}],
              'overlays':[{'id':'alpha','file':str(alpha),'start':.1,'mode':'alpha'},
                          {'id':'full','file':full,'start':.4,'mode':'full'},
                          {'id':'missing','file':str(root/'missing.mov'),'start':0,'mode':'alpha'}],
              'sfx':[{'file':str(sound),'start':.2,'gain_db':-18},
                     {'file':str(root/'missing.mp3'),'start':0,'gain_db':-18}],
              'music':{'file':str(music),'gain_db':-26,'duck':True}}
    spec = root/'overlays.json'
    spec.write_text(json.dumps(config),encoding='utf-8')
    output = root/'composite.mp4'
    result = subprocess.run([sys.executable,'scripts/composite.py','--input',base,'--overlays',str(spec),
                             '--output',str(output),'--work-dir',str(root/'composite-work'),'--preview'],
                            capture_output=True,text=True,encoding='utf-8')
    if result.returncode:
        raise RuntimeError(result.stderr+result.stdout)
    manifest = json.loads((root/'composite-work/composite-result.json').read_text(encoding='utf-8'))
    assert abs(manifest['prepend_shift_s']-.3)<1e-8
    assert len(manifest['warnings'])==2
    assert abs(float(probe(output)['format']['duration'])-1.6)<.05
    decoded = subprocess.run(['ffmpeg','-v','error','-i',str(output),'-f','null','-'],capture_output=True)
    assert decoded.returncode==0 and not decoded.stderr.strip(),decoded.stderr
    def pixel(t):
        raw = subprocess.check_output(['ffmpeg','-v','error','-ss',str(t),'-i',str(output),
                                       '-frames:v','1','-vf','scale=1:1','-f','rawvideo','-pix_fmt','rgb24','-'])
        return np.frombuffer(raw,dtype=np.uint8)
    red,blue = pixel(.45),pixel(.75)
    assert red[0]>red[1]+35 and red[0]>red[2]+35,red
    assert blue[2]>180 and blue[0]<40,blue
    original_output = output
    output = windowed_video(root/'composite-work/joined.mkv',
                            [dict(x,metadata=probe(x['file'])) for x in config['overlays'][:2]],
                            .3,1.6,root/'composite-work',encoder(True),960,540,window_s=.5)
    assert int(next(x for x in probe(output)['streams'] if x['codec_type']=='video')['nb_frames'])==48
    for t in [.45,.55]:
        red = pixel(t)
        assert red[0]>red[1]+35 and red[0]>red[2]+35,(t,red)
    blue = pixel(.75)
    assert blue[2]>180 and blue[0]<40,blue
    output = original_output
    # Verify that prepend/append clip tones survived, not just their video.
    raw = subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-vn','-ac','1',
                                   '-ar','48000','-f','f32le','-'])
    sound_data = np.frombuffer(raw,dtype='<f4')
    for start,hz in [(.05,1000),(1.15,1600)]:
        chunk = sound_data[round(start*48000):round((start+.1)*48000)]
        peak = np.fft.rfftfreq(len(chunk),1/48000)[np.argmax(abs(np.fft.rfft(chunk)))]
        assert abs(peak-hz)<15,(start,peak)
    print('PASS composite: alpha, full, prepend shift, SFX/music ducking graph, audio/no-audio append, missing-media skip, decoded output and preserved clip audio',flush=True)


if __name__=='__main__':
    main()
