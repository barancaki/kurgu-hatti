"""Estimate screen_time = webcam_time - offset from offline audio."""
import argparse
import json
from pathlib import Path
import subprocess

import numpy as np
from scipy import signal

from common import audio, output_path, rms_frames, write_json


def envelope(samples, sr, feature_sr=1000):
    band = signal.sosfiltfilt(signal.butter(4, [180, min(3500, sr * .45)],
                                         btype='bandpass', fs=sr, output='sos'), samples)
    env = signal.sosfiltfilt(signal.butter(3, 40, fs=sr, output='sos'), np.abs(band))
    return signal.resample_poly(env, feature_sr, sr).astype('float64')


def match(reference, search):
    """Normalized valid FFT correlation; return search index, Pearson r, peak separation."""
    reference = reference.astype('float64') - np.mean(reference)
    search = search.astype('float64')
    n = len(reference)
    if len(search) < n or np.linalg.norm(reference) < 1e-10:
        raise ValueError('Insufficient audio or constant reference')
    sums = np.r_[0., np.cumsum(search)]
    sq = np.r_[0., np.cumsum(search ** 2)]
    energy = (sq[n:] - sq[:-n]) - (sums[n:] - sums[:-n]) ** 2 / n
    corr = signal.correlate(search, reference, mode='valid', method='fft')
    corr /= np.sqrt(np.maximum(energy, 1e-20) * np.sum(reference ** 2))
    best = int(np.argmax(corr))
    return best, float(corr[best]), corr


def residual_lag(a, b, sr, limit=.2):
    """Full correlation, normalized for actual overlap; positive = a is later."""
    a = a.astype('float64') - a.mean()
    b = b.astype('float64') - b.mean()
    corr = signal.correlate(a, b, method='fft')
    ea = signal.correlate(a * a, np.ones(len(b)), method='fft')
    eb = signal.correlate(np.ones(len(a)), b * b, method='fft')
    corr /= np.sqrt(np.maximum(ea * eb, 1e-20))
    lags = signal.correlation_lags(len(a), len(b))
    valid = np.abs(lags) <= round(limit * sr)
    idx = np.flatnonzero(valid)[np.argmax(corr[valid])]
    return float(lags[idx] / sr), float(corr[idx])



def inspect_local_drift(web, screen, sr, slope, intercept):
    """Short checks distinguish a gradual clock slope from abrupt alignment steps."""
    sos = signal.butter(3, [200, 3200], btype='bandpass', fs=sr, output='sos')
    wb = signal.sosfiltfilt(sos, web)
    sb = signal.sosfiltfilt(sos, screen)

    def measure(t, length=2):
        estimate = intercept + slope * (t + length / 2)
        wi = round(t * sr)
        si = round((t - estimate - .12) * sr)
        ref = wb[wi:wi + round(length * sr)]
        search = sb[si:si + len(ref) + round(.24 * sr)]
        if si < 0 or len(search) < len(ref):
            return None
        index, score, _ = match(ref, search)
        return {'webcam_start_s': t, 'window_s': length, 'screen_offset_s': wi / sr - (si + index) / sr,
                'correlation': score}

    start = max(12., intercept + 3)
    end = min(len(web) / sr - 5, len(screen) / sr + intercept - 5)
    checks = [measure(float(t)) for t in np.arange(start, end, 15)]
    checks = [r for r in checks if r and r['correlation'] >= .35]
    groups = []
    for row in checks:
        if not groups or abs(row['screen_offset_s'] - np.median(
                [r['screen_offset_s'] for r in groups[-1]])) > .03:
            groups.append([])
        groups[-1].append(row)
    plateaus = [{'offset_s': float(np.median([r['screen_offset_s'] for r in group])),
                 'spread_ms': float(np.ptp([r['screen_offset_s'] for r in group]) * 1000),
                 'first_sample_s': group[0]['webcam_start_s'], 'last_sample_s': group[-1]['webcam_start_s'],
                 'sample_count': len(group)} for group in groups]
    stepped = (len(plateaus) >= 2 and all(p['sample_count'] >= 3 and p['spread_ms'] <= 12 for p in plateaus))
    transitions = []
    if stepped:
        for left, right in zip(plateaus, plateaus[1:]):
            fine = [measure(float(t), .5) for t in np.arange(left['last_sample_s'],
                                                             right['first_sample_s'] + .01, .25)]
            fine = [r for r in fine if r and r['correlation'] >= .45]
            old = [r['webcam_start_s'] for r in fine if abs(r['screen_offset_s'] - left['offset_s']) < .008]
            new = [r['webcam_start_s'] for r in fine if abs(r['screen_offset_s'] - right['offset_s']) < .008]
            lower = max(old) if old else left['last_sample_s']
            upper = min(new) + .5 if new else right['first_sample_s'] + 2
            transitions.append({'webcam_transition_bracket_s': [lower, max(lower, upper)],
                                'offset_before_s': left['offset_s'], 'offset_after_s': right['offset_s'],
                                'step_ms': (right['offset_s'] - left['offset_s']) * 1000})
    residuals = [(r['screen_offset_s'] - intercept - slope * (r['webcam_start_s'] + 1)) * 1000 for r in checks]
    return {'pattern': 'piecewise_constant_steps' if stepped else 'varying_offset',
            'short_window_measurements': checks, 'plateaus': plateaus if stepped else [],
            'transition_brackets': transitions,
            'linear_model_max_error_ms': float(max(abs(r) for r in residuals)) if residuals else None,
            'interpretation': ('Short windows show stable plateaus and abrupt jumps; a single clock-stretch model is not exact. '
                               'This suggests capture/buffer/sample corrections, but audio alone cannot prove their origin.'
                               if stepped else 'Short-window checks do not establish a stepped pattern.')}


def packet_timestamps(video):
    data = json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
         '-show_entries', 'packet=pts_time,duration_time', '-of', 'json', str(video)], text=True))
    packets = data['packets']
    gaps = [float(b['pts_time']) - float(a['pts_time']) - float(a.get('duration_time', 0))
            for a, b in zip(packets, packets[1:]) if 'pts_time' in a and 'pts_time' in b]
    return {'packets': len(packets), 'gaps_over_3ms': sum(abs(g) > .003 for g in gaps),
            'max_gap_ms': float(max(abs(g) for g in gaps) * 1000) if gaps else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--webcam-audio', required=True)
    parser.add_argument('--screen-audio', required=True)
    parser.add_argument('--webcam-video', required=True, help='Original video for independent ffmpeg sign verification')
    parser.add_argument('--screen-video', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--verify-dir', required=True, help='Directory for the two 2-second WAV evidence clips')
    parser.add_argument('--window-seconds', type=float, default=60)
    args = parser.parse_args()
    if args.window_seconds < 5:
        parser.error('--window-seconds must be >= 5')
    inputs = [args.webcam_audio, args.screen_audio, args.webcam_video, args.screen_video]
    out = output_path(args.output, inputs)
    clips = [output_path(Path(args.verify_dir) / f'{name}-2s.wav', inputs + [out])
             for name in ['webcam', 'screen']]
    web, sr = audio(args.webcam_audio, 16000)
    screen, _ = audio(args.screen_audio, sr)
    feature_sr = 1000
    we = envelope(web, sr, feature_sr)
    se = envelope(screen, sr, feature_sr)
    full = signal.correlate(we - we.mean(), se - se.mean(), method='fft')
    lags = signal.correlation_lags(len(we), len(se))
    coarse = float(lags[np.argmax(full)] / feature_sr)
    overlap_start = max(0, coarse) + 3
    overlap_end = min(len(web) / sr, len(screen) / sr + coarse) - 3
    window = args.window_seconds
    if overlap_end - overlap_start < window * 3:
        raise ValueError('At least three non-overlapping measurement windows are required')
    starts = [overlap_start, (overlap_start + overlap_end - window) / 2, overlap_end - window]
    measurements = []
    for label, start in zip(['beginning', 'middle', 'end'], starts):
        wi = round(start * feature_sr)
        si = max(0, round((start - coarse - 2) * feature_sr))
        ref = we[wi:wi + round(window * feature_sr)]
        search = se[si:min(len(se), si + len(ref) + 4 * feature_sr)]
        index, env_score, corr = match(ref, search)
        estimate = wi / feature_sr - (si + index) / feature_sr
        # Refine to the audio sample grid using bandpassed waveform correlation.
        wb = signal.sosfiltfilt(signal.butter(3, [200, 3200], btype='bandpass', fs=sr, output='sos'),
                               web[round(start * sr):round((start + window) * sr)])
        sb_start = max(0, round((start - estimate - .08) * sr))
        sb = signal.sosfiltfilt(signal.butter(3, [200, 3200], btype='bandpass', fs=sr, output='sos'),
                               screen[sb_start:sb_start + len(wb) + round(.16 * sr)])
        fine, wave_score, _ = match(wb, sb)
        refined = round(start * sr) / sr - (sb_start + fine) / sr
        use_wave = wave_score >= .2 and abs(refined - estimate) < .075
        offset = refined if use_wave else estimate
        away = np.abs(np.arange(len(corr)) - index) > 250
        second = float(np.max(corr[away])) if away.any() else 0
        measurements.append({'position': label, 'webcam_start_s': start, 'webcam_center_s': start + window / 2,
                             'window_s': window, 'screen_offset_s': offset, 'envelope_offset_s': estimate,
                             'envelope_correlation': env_score, 'waveform_correlation': wave_score,
                             'waveform_refinement_used': use_wave,
                             'peak_margin': env_score - second})
    centers = np.array([m['webcam_center_s'] for m in measurements])
    offsets = np.array([m['screen_offset_s'] for m in measurements])
    slope, intercept = np.polyfit(centers, offsets, 1)
    spread_ms = float(np.ptp(offsets) * 1000)
    stable = spread_ms <= 40
    reported = float(np.median(offsets)) if stable else float(intercept)
    applied_slope = 0. if stable else float(slope)
    local_drift = inspect_local_drift(web, screen, sr, slope, intercept) if not stable else None
    if local_drift and local_drift['pattern'] == 'piecewise_constant_steps':
        reported = local_drift['plateaus'][0]['offset_s']
    # Pick a speech-rich two-second moment in the middle measurement.
    mid = starts[1]
    levels, hop = rms_frames(web, sr, .1)
    candidates = np.arange(round(mid / hop), round((mid + window - 2) / hop))
    chosen = int(candidates[np.argmax(np.convolve(levels, np.ones(20), mode='same')[candidates])])
    web_time = chosen * hop
    # Independently measure the exact short-clip offset rather than assuming the linear model.
    reference = web[round(web_time * sr):round((web_time + 2) * sr)]
    estimate = float(intercept + slope * web_time)
    screen_index = round((web_time - estimate - .12) * sr)
    search = screen[screen_index:screen_index + len(reference) + round(.24 * sr)]
    best, clip_score, _ = match(reference, search)
    clip_offset = round(web_time * sr) / sr - (screen_index + best) / sr
    screen_time = web_time - clip_offset
    for video, start, dest in zip([args.webcam_video, args.screen_video], [web_time, screen_time], clips):
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                        '-ss', f'{start:.6f}', '-i', str(video), '-t', '2', '-vn',
                        '-ac', '1', '-ar', str(sr), '-c:a', 'pcm_s16le', str(dest)], check=True)
    va, _ = audio(clips[0], sr)
    vb, _ = audio(clips[1], sr)
    verify_lag, verify_corr = residual_lag(va, vb, sr)
    verified = abs(verify_lag) <= .04 and verify_corr >= .2
    scores = [m['envelope_correlation'] for m in measurements]
    confidence = float(np.clip(min(scores) * (1 if stable else .8) * (1 if verified else .3), 0, 1))
    result = {'screen_offset_s': reported, 'meaning': 'screen_time = webcam_time - offset_at(webcam_time); screen_offset_s is the initial measured offset; consult local_drift for time-varying offsets',
              'drift_ms_per_min': float(slope * 60000),
              'drift_definition': 'positive drift means webcam_time - screen_time increases over time',
              'offset_model': {'intercept_s': float(intercept) if not stable else reported, 'slope_s_per_s': applied_slope,
                               'meaning': 'screen_time = webcam_time - (intercept_s + slope_s_per_s * webcam_time)'},
              'local_drift': local_drift,
              'packet_timestamp_diagnostics': {'webcam': packet_timestamps(args.webcam_video), 'screen': packet_timestamps(args.screen_video)},
              'confidence': confidence, 'confidence_definition': 'minimum window envelope Pearson correlation, penalized for inconsistency/failed verification; not a calibrated probability',
              'coarse_offset_s': coarse, 'measurements': measurements, 'spread_ms': spread_ms,
              'within_40ms': stable, 'linear_fit_residual_ms': ((offsets - (intercept + slope * centers)) * 1000).tolist(),
              'drift_note': ('Window offsets agree within 40 ms; constant offset recommended.' if stable else
                             'Offsets differ by >40 ms; inspect local_drift. The reported linear drift is an average trend and may conceal discrete timing steps. Packet diagnostics cannot establish capture-buffer behavior.'),
              'sign_verification': {'method': 'ffmpeg extracts two corresponding 2s clips from original videos; normalized full waveform FFT correlation',
                                    'webcam_start_s': web_time, 'screen_start_s': screen_time, 'measured_clip_offset_s': clip_offset,
                                    'residual_lag_s': verify_lag, 'correlation': verify_corr, 'passed': verified,
                                    'clips': [str(p) for p in clips]},
              'audio_durations_s': {'webcam': len(web) / sr, 'screen': len(screen) / sr}}
    write_json(out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not verified:
        raise SystemExit('Sign verification failed; sync.json contains diagnostic measurements.')


if __name__ == '__main__':
    main()
