"""Shared local ffmpeg process helpers for the two render stages."""
import json
import math
from pathlib import Path
import subprocess


def run(args, log=None):
    command = ['ffmpeg', '-hide_banner', '-nostdin', '-y', '-threads', '2'] + [str(a) for a in args]
    result = subprocess.run(command, capture_output=True)
    if log:
        Path(log).write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-6000:])
    return result.stderr.decode('utf-8', errors='replace')


def probe(path):
    return json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], text=True))


def encoder(preview=False):
    import settings
    video = settings.load()['video']
    test = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                           '-i', 'color=s=256x256:r=30', '-frames:v', '1', '-c:v', 'h264_nvenc',
                           '-f', 'null', '-'], capture_output=True)
    if test.returncode == 0:
        return ['-c:v', 'h264_nvenc', '-preset', 'p2' if preview else 'p6',
                '-cq', str(video['preview_nvenc_cq'] if preview else video['nvenc_cq']), '-b:v', '0']
    return ['-c:v', 'libx264', '-preset', 'veryfast' if preview else 'medium',
            '-crf', str(video['preview_x264_crf'] if preview else video['x264_crf'])]


def video_options(enc, fps=30):
    return enc + ['-pix_fmt', 'yuv420p', '-r', str(fps), '-fps_mode', 'cfr', '-g', str(fps*2),
                  '-bf', '2', '-video_track_timescale', '90000', '-color_primaries', 'bt709',
                  '-color_trc', 'bt709', '-colorspace', 'bt709']


def concat_file(paths, target, durations=None):
    if durations is not None and (len(durations) != len(paths) or any(not math.isfinite(d) or d <= 0 for d in durations)):
        raise ValueError('Concat durations must match files and be positive finite seconds')
    lines = []
    for i, path in enumerate(paths):
        lines.append("file '" + Path(path).resolve().as_posix().replace("'", "'\\''") + "'\n")
        if durations is not None:
            # Container duration rounds to milliseconds; repeated rounding corrupts the 30fps DTS grid.
            lines.append(f'duration {durations[i]:.9f}\n')
    Path(target).write_text(''.join(lines), encoding='utf-8')


def json_measure(log):
    start = log.rfind('{')
    if start < 0:
        raise ValueError('No loudness JSON in ffmpeg log')
    return json.loads(log[start:log.find('}', start)+1])
