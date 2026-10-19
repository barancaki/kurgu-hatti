"""Central pipeline settings.

Every script reads the same settings: built-in defaults below, overridden by a JSON file
(``--config PATH``, else ``$KURGU_CONFIG``, else ``./kurgu.config.json`` if present).
Only the keys you want to change need to appear in the file; nested sections are merged.
"""
import copy
import json
import os
from pathlib import Path

# Language presets: filler sounds, words whose doubling is safe to trim, ambiguous fillers
# that need human review, emphatic repeats that must survive, and "cut this" phrases.
LEXICONS = {
    'tr': {
        'turkish_casing': True,
        'initial_prompt': 'Türkçe konuşma. Dolgu sesleri ve tereddütler: eee, ııı, ee, ım, hmm, şey. '
                          'İngilizce teknik terimler kullanılabilir.',
        'filler_regex': r'(e{2,}|ı{2,}|i{2,}|[ıi]m|h+m+)',
        'safe_repeat_words': ['bu', 'bunu', 'ben', 'biz', 'şimdi', 'yani', 've', 'ama', 'burada', 'buradan', 'böyle'],
        'emphatic_repeat_words': ['çok', 'garip', 'gibi', 'vesaire'],
        'ambiguous_fillers': ['şey'],
        'ambiguous_keep_before': ['bir', 'her', 'hiçbir', 'çok', 'bütün', 'bu', 'o', 'şu', 'aynı', 'diye', 'gibi'],
        'ambiguous_keep_after': ['var', 'yok', 'değil', 'oldu', 'olabilir', 'olacak', 'yapmak'],
        'retake_phrases': ['keseceğiz', 'burayı kes', 'tekrar başla', 'baştan al', 'dur bekle',
                           'ikisi de başladı', 'böyle kapatalım'],
    },
    'en': {
        'turkish_casing': False,
        'initial_prompt': 'English speech. Fillers and hesitations: um, uh, erm, hmm, like. '
                          'Technical terms may appear.',
        'filler_regex': r'(u+m+|u+h+|e+r+m*|a+h+|h+m+|m+h*m+)',
        'safe_repeat_words': ['the', 'a', 'and', 'so', 'i', 'we', 'this', 'that', 'it', 'to', 'now'],
        'emphatic_repeat_words': ['very', 'really', 'so', 'no'],
        'ambiguous_fillers': ['like'],
        'ambiguous_keep_before': ['looks', 'look', 'feels', 'feel', 'just', 'would', 'i', 'you', 'we', 'they', 'not'],
        'ambiguous_keep_after': ['this', 'that', 'it', 'a', 'the'],
        'retake_phrases': ['cut that', 'cut this', 'let me start over', 'start again', 'take two', 'one more time'],
    },
}

DEFAULTS = {
    'language': 'tr',              # Whisper language code and lexicon preset (tr is the tested one)
    'lexicon': {},                 # per-key overrides of the language preset
    'transcribe': {'model': 'large-v3', 'beam_size': 5, 'cpu_threads': 8},
    'cuts': {
        'silence_gap_s': 0.45,     # silences longer than this are shortened
        'breath_s': 0.20,          # quiet kept inside a shortened silence
        'filler_min_s': 0.40,      # wordless voiced audio at least this long counts as a filler
        'filler_padding_s': [0.10, 0.14],
        'min_keep_s': 0.25,
        'closing_handle_s': 0.60,  # keep after the last word
    },
    'audio': {
        'target_lufs': -14.0,      # final integrated loudness
        'true_peak_dbtp': -1.0,    # ceiling for the delivered AAC
        'lufs_tolerance': 0.5,
        'crossfade_s': 0.012,
        'sfx_gain_db': -18.0,
        'music_gain_db': -26.0,
    },
    'music': {'speech_lufs': -33.0, 'title_lufs': -20.0},
    'video': {'fps': 30, 'nvenc_cq': 19, 'x264_crf': 18, 'preview_nvenc_cq': 24, 'preview_x264_crf': 23},
    'colors': {                    # ffmpeg colors (0xRRGGBB); brand them in your config
        'accent': '0xFFC400',      # Shorts divider dot and active caption word
        'divider': '0x0B0B0B',
        'screen_fill': '0x111410', # fill behind anchored screen zooms
        'contact_sheet': '0x171717',
    },
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def add_config_arg(parser):
    parser.add_argument('--config', help='Pipeline settings JSON (default: $KURGU_CONFIG or ./kurgu.config.json)')
    return parser


def load(path=None):
    settings = copy.deepcopy(DEFAULTS)
    path = path or os.environ.get('KURGU_CONFIG') or ('kurgu.config.json' if Path('kurgu.config.json').is_file() else None)
    if path:
        _merge(settings, json.loads(Path(path).read_text(encoding='utf-8')))
        # Helpers and child processes (render_common, check scripts) see the same file.
        os.environ['KURGU_CONFIG'] = str(Path(path).resolve())
    language = settings['language']
    lexicon = copy.deepcopy(LEXICONS.get(language, LEXICONS['en']))
    settings['lexicon'] = _merge(lexicon, settings.get('lexicon') or {})
    return settings


def loudnorm(peak_headroom_db=0.7, cfg=None):
    """ffmpeg loudnorm target; headroom below the delivery ceiling protects against AAC overshoot."""
    audio = (cfg or load())['audio']
    return f"loudnorm=I={audio['target_lufs']:g}:TP={audio['true_peak_dbtp'] - peak_headroom_db:g}:LRA=11"


def loudness_ok(measure, cfg=None):
    audio = (cfg or load())['audio']
    return (abs(float(measure['input_i']) - audio['target_lufs']) <= audio['lufs_tolerance']
            and float(measure['input_tp']) <= audio['true_peak_dbtp'])


def ass_bgr(color):
    """0xRRGGBB -> BBGGRR for ASS subtitle colour tags."""
    value = color.lower().removeprefix('0x').removeprefix('#')
    return (value[4:6] + value[2:4] + value[0:2]).upper()


def lexicon(language='tr'):
    """Preset lexicon without reading any file (used by tests and library calls)."""
    return copy.deepcopy(LEXICONS[language])
