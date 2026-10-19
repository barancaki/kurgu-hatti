"""Small offline checks for bar selection, equal-power joins, bounded ducking and ramps."""
import subprocess
import sys

import numpy as np

from music_bed import analyze, plan, assemble
from mix_music import duck_envelope, gain_knots


def main():
    sr=12000
    t=np.arange(sr*80)/sr
    phase=np.mod(t-.1,.625)
    beat=np.exp(-phase/.055)
    carrier=np.sin(2*np.pi*180*t)+.2*np.sin(2*np.pi*360*t)
    envelope=np.where(t<10,.15,1.)*np.minimum(1,np.maximum(0,(80-t)/8))
    mono=(.3*carrier*(.1+beat)*envelope).astype('float32')
    samples=np.column_stack([mono,mono])
    info=analyze(samples,sr,96)
    assert abs(info['bpm']-96)<.1,info['bpm']
    assert info['beat_confidence']>.4
    pieces,fade=plan(info,samples,sr,140)
    body=[p for p in pieces if p['role']=='body-variation']
    assert len(body)>=2 and len({round(p['entry_bar_s'],4) for p in body})==len(body)
    bar=info['bar_s']
    for p in pieces:
        for key in ['entry_bar_s','exit_bar_s']:
            if key in p:
                grid=(p[key]-info['downbeat_phase_s'])/bar
                assert abs(grid-round(grid))<1e-6
    output,joins=assemble(samples,sr,pieces,fade)
    assert np.isfinite(output).all() and abs(len(output)/sr-140)<bar/2
    assert np.max(abs(output))<=np.max(abs(samples))*1.01
    try:
        plan(info,samples,sr,10)
        raise AssertionError('Too-short target accepted')
    except ValueError:
        pass
    key=np.sin(2*np.pi*1000*np.arange(sr*3)/sr).astype('float32')*.5
    times,reduction=duck_envelope(key,sr,3,0,.25,1)
    assert 4.99<=reduction.max()<=5
    assert reduction[times>2.8].max()<1
    times=np.linspace(0,30,3001)
    gain=gain_knots(times,30,5,4,[-20,-33,-20])
    assert np.isfinite(gain).all()
    for t,value in [(5,-20),(5.5,-26.5),(6,-33),(25,-33),(25.5,-26.5),(26,-20)]:
        assert abs(gain[round(t*100)]-value)<1e-5,(t,gain[round(t*100)])
    assert max(abs(np.diff(gain)))<.21
    for script in ['music_bed','mix_music','qa_music']:
        p=subprocess.run([sys.executable,f'scripts/{script}.py','--help'],capture_output=True)
        assert p.returncode==0,p.stderr
    print('PASS tempo/bar phase, varied entries, exact-power coherent joins, duration, short-target rejection, 5dB duck limit, smooth ramps and CLI help')


if __name__=='__main__':
    main()
