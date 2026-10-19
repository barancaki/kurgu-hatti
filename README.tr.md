# Kurgu Hattı — webcam + ekran videosu kurgu hattı

*English: [README.md](README.md)*

Ham webcam kaydı ve ekran kaydından kurgulanmış YouTube videosu ve dikey Shorts üretir. Sen kurguyu düşünmeden konuşursun; hat transkript çıkarır, iki kaydı senkronlar, sessizlik ve dolguları keser, yüz/ekran düzenini render eder, sesi miksler, animasyonlarını bindirir ve sonucu kontrol eder. Yayından önce son onay insanda.

Scriptler düz Python + ffmpeg'dir ve tek başına çalışır. Yapay zekâ ajanları (herhangi bir LLM) opsiyoneldir: scriptlerin okuduğu karar dosyalarını (neyi keselim, neyi canlandıralım) doldururlar.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Özellikler

- faster-whisper ile **çevrimdışı, kelime düzeyinde transkript**; dolgu seslerini yazmaya teşvik eden istem.
- Webcam ve ekran kaydının **ses tabanlı, parça parça senkronu**.
- **Otomatik kesim önerisi**: 0,45 sn'den uzun sessizlik kısalır, ≥ 0,40 sn kelimesiz ses ("eee") kesilir, vurgu ikilemeleri kalır, emin olunmayan yerler inceleme listesine gider. Kelime ortasından asla kesmez.
- **Düzen render'ı**: TALK (yüz tam kare, kesimlerde dönüşümlü zoom) ve SCREEN (ekran tam kare + yuvarlak webcam PiP), aralarında yumuşak geçiş, anahtar kareli zoom.
- **Yayın standardında ses**: her kesimde crossfade, temizleme zinciri, iki geçişli loudnorm −14 LUFS / −1 dBTP (ayarlanabilir).
- **Birleştirme**: saydam overlay videolar, intro/outro, SFX ve kendiliğinden kısılan müzik tek geçişte.
- **Shorts**: ham kaynaklardan dikey yeniden çerçeveleme, kelime zamanlı altyazı, kanca/CTA, ek animasyon katmanı.
- **QA**: tam decode, ses seviyesi, senkron korelasyonu, gözle inceleme için etiketli kare panoları.
- Eşikler, ses seviyesi, kalite ve renkler için **tek ayar dosyası**; dil önayarları (`tr` test edildi, `en` başlangıç).

## İstasyonlar

| # | İstasyon | Script / girdi | Çıktı |
|---|---|---|---|
| 1 | Kayıt | sen | `raw/webcam.mp4`, `raw/screen.mp4` |
| 2 | Transkript | `transcribe.py` | `work/transcript.json`, `.srt` |
| 3 | Senkron | `sync.py` | `work/sync.json` |
| 4 | Otomatik kesim | `cutlist.py` | `work/edl.json` (kesimler + inceleme listesi) |
| 5 | Kurgu kararı | insan / LLM ([istem](templates/prompts/edit-decision.md)) | `edit/manual_cuts.json`, `edit/layout.json` |
| 6 | Final EDL | `finalize_edl.py`, `timemap.py` | `edit/final_edl.json`, kurgulu transkript |
| 7 | Base render | `render_base.py` | `renders/base.mp4` |
| 8 | Animasyon | kendi aracın ([istem](templates/prompts/animation-plan.md), [şablon](templates/overlay-spec.md)) | `overlays/*.mov`, `edit/overlays.json` |
| 9 | Birleştirme + QA | `composite.py`, `qa_final.py`, `mix_music.py` | `renders/final.mp4`, `qa/` |
| 10 | Shorts | `make_shorts.py` | `renders/shorts/` |
| 11 | Onay + yayın | sen | — |
| ↻ | Hafıza | [stil rehberi](templates/style-guide.md) | onaylanan her revize kurala dönüşür |

Mimari şeması: [README.md](README.md#architecture).

## Önerilen iş bölümü

Hat belirli bir yapay zekâya bağlı değildir. İyi çalışan bir düzen:

- **Editör olarak bir LLM** (ör. Claude): transkripti ve inceleme listesini okur, `manual_cuts.json` / `layout.json` yazar, animasyonları planlar, kare panolarını inceler, revizeleri stil rehberine kural olarak yazar.
- **Operatör olarak bir kodlama ajanı** (ör. Codex): scriptleri çalıştırır, yolları düzeltir, raster görsel üretir.
- **Animatör olarak bir model ya da insan**: overlay'leri üretir, ör. [HyperFrames](https://github.com/heygen-com/hyperframes) (HTML + GSAP → video) ile saydam ProRes 4444.
- **İnsan**: kayıt, geri bildirim, onay.

Her adım elle de yapılabilir.

## Gereksinimler

- Python 3.11+
- `PATH`'te ffmpeg ve ffprobe (libx264, ProRes, loudnorm, ebur128)
- Yerelde indirilmiş faster-whisper modeli (`large-v3` önerilir); transkript adımı model indirmez
- Opsiyonel: NVIDIA GPU (CUDA'da Whisper, NVENC kodlama; yoksa CPU / libx264)
- Opsiyonel: saydam video çıkaran bir animasyon aracı
- Opsiyonel: kendi SFX (`sfx/library/`) ve müzik (`music/`) dosyaların; repoda ses dosyası yok

## Kurulum

```bash
git clone https://github.com/barancaki/kurgu-hatti.git
cd kurgu-hatti
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Hızlı başlangıç

Her video kendi klasöründe; komutlar o klasörden çalıştırılır. Tam komut sırası: [README.md › Quick start](README.md#quick-start). Kısaca:

```bash
K=../kurgu-hatti/scripts
python $K/transcribe.py --audio work/webcam.wav --source-video raw/webcam.mp4 --model-root models --output work/transcript.json --srt work/webcam.srt
python $K/sync.py --webcam-audio work/webcam.wav --screen-audio work/screen.wav --webcam-video raw/webcam.mp4 --screen-video raw/screen.mp4 --output work/sync.json --verify-dir work/sync-verify
python $K/cutlist.py --transcript work/transcript.json --audio work/webcam.wav --output work/edl.json
# kurgu kararı: edit/manual_cuts.json + edit/layout.json
python $K/finalize_edl.py --edl work/edl.json --transcript work/transcript.json --audio work/webcam.wav --manual-cuts edit/manual_cuts.json --output edit/final_edl.json
python $K/render_base.py --edl edit/final_edl.json --layout edit/layout.json --output renders/base.mp4 --work-dir work/segments
python $K/composite.py --input renders/base.mp4 --overlays edit/overlays.json --output renders/final.mp4 --work-dir work/composite
python $K/make_shorts.py --config edit/shorts.json
```

## Ayarlar

Tek JSON dosyası: eşikler (sessizlik 0,45 sn, dolgu 0,40 sn, pay), ses seviyesi (−14 LUFS, −1 dBTP), müzik seviyeleri, kodlama kalitesi, renkler ve dil sözlüğü. `--config` ile ver, `KURGU_CONFIG` ortam değişkenini ayarla ya da proje klasörüne `kurgu.config.json` koy. Türkçe için `"language": "tr"` (varsayılan). Ayrıntı: [docs/configuration.md](docs/configuration.md) (İngilizce).

## Testler

```bash
python scripts/check_analysis.py
python scripts/check_final_edit.py
python scripts/check_music.py
python scripts/make_shorts.py --self-check
python -m unittest discover -s tools -p "test_*.py"
```

## Katkı

[CONTRIBUTING.md](CONTRIBUTING.md) (İngilizce; Türkçe issue/PR da kabul).

## Lisans

[MIT](LICENSE) © 2026 Baran Çakı
