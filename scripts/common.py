"""Shared offline audio and output utilities."""
import json
import os
import re
from pathlib import Path
import subprocess

import numpy as np
import soundfile as sf
from scipy import signal


def output_path(value, inputs=()):
    dest = Path(value).resolve()
    if any(p.name.lower() == 'raw' for p in dest.parents):
        raise ValueError('Outputs must not be written inside raw/')
    if dest in {Path(p).resolve() for p in inputs if p}:
        raise ValueError('Output must differ from every input')
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def write_json(path, data):
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(temp, path)


def duration(path):
    data = json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
        text=True))
    return float(data['format']['duration'])


def audio(path, target_sr=None):
    data, sr = sf.read(path, dtype='float32', always_2d=True)
    data = data.mean(axis=1)
    if not len(data) or not np.isfinite(data).all():
        raise ValueError('Audio must be nonempty and finite')
    if target_sr and target_sr != sr:
        from math import gcd
        factor = gcd(sr, target_sr)
        data = signal.resample_poly(data, target_sr // factor, sr // factor)
        sr = target_sr
    return data, sr


def rms_frames(data, sr, hop_s=.005):
    hop = max(1, round(sr * hop_s))
    count = len(data) // hop
    rms = np.sqrt(np.mean(data[:count * hop].reshape(count, hop).astype('float64') ** 2, axis=1))
    return rms, hop / sr


def timestamp(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def join_words(words):
    text = " ".join(w["word"].strip() for w in words)
    return re.sub(r"\s+([.,:;!?\u0027’])", r"\1", text)
