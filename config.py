import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
# Kalıcı veri dizini (data.db, config.json) — Docker volume (DATA_DIR ile override).
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Frame'ler KALICI diskte (volume) tutulur ki deploy/restart'ta kaybolmasın.
# Yalnızca "kanıt" (reklam çıkan) kareler saklanır + toplam boyut cap'i ile
# eski videoların kareleri otomatik temizlenir (volume dolmasın). FRAMES_DIR
# env'i ile override edilebilir.
FRAMES_DIR = Path(os.environ.get("FRAMES_DIR", DATA_DIR / "frames"))
FRAMES_DIR.mkdir(parents=True, exist_ok=True)

# Görüntü tespit modeli. ÜCRETSIZ KATMAN günlük istek (RPD) kotaları:
#   gemini-2.5-flash / 2.5-flash-lite  → sadece 20 RPD (günde ~2-3 video!)
#   gemini-3.1-flash-lite              → 15 RPM + 500 RPD (~100 video/gün) ✓
# Bu yüzden varsayılan 3.1-flash-lite. Vision destekli, marka yazısını okuyor.
# Ücretli katmandaysan tam doğruluk için: GEMINI_MODEL=gemini-2.5-flash
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
# Hafif/ucuz görev (metin-only marka çıkarımı) için — aynı yüksek-RPD havuzu
GEMINI_MODEL_LITE = os.environ.get("GEMINI_MODEL_LITE", "gemini-3.1-flash-lite")


def gemini_url(model=None):
    """Belirtilen model için generateContent endpoint URL'i."""
    return (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model or GEMINI_MODEL}:generateContent"
    )


# Geriye dönük uyumluluk — eski importlar için
GEMINI_URL = gemini_url(GEMINI_MODEL)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── Analiz performans/maliyet ayarları (env ile override edilebilir) ──
# Frame örnekleme aralığı (saniye)
FRAME_INTERVAL = _int_env("FRAME_INTERVAL", 8)
# Gemini'ye gönderilen frame genişliği (px) — küçük = ucuz, büyük = daha okunur
FRAME_WIDTH = _int_env("FRAME_WIDTH", 640)
# Tek Gemini çağrısında kaç frame analiz edilsin — büyük = daha az istek (RPD dostu)
BATCH_SIZE = _int_env("BATCH_SIZE", 12)
# Gemini dakika başı istek bütçesi. Ücretsiz katman RPM: 3.1-flash-lite=15,
# diğer *-lite=10, Flash=5. Modele göre güvenli varsayılan; GEMINI_RPM env'i ile
# override edilebilir (ücretli katmanda 300+ ver → bekleme neredeyse sıfırlanır).
_m = GEMINI_MODEL.lower()
if "3.1-flash-lite" in _m:
    _default_rpm = 15
elif "lite" in _m:
    _default_rpm = 10
else:
    _default_rpm = 5
GEMINI_RPM = _int_env("GEMINI_RPM", _default_rpm)
# Sahne-değişimi eşiği: ardışık frame farkı bunun altındaysa aynı sahne sayılır
SCENE_DIFF_THRESHOLD = _float_env("SCENE_DIFF_THRESHOLD", 0.03)
# Alt-bant yükseltici: atlanacak frame'in alt bandı bu kadar değiştiyse yine de gönder
LOWER_BAND_THRESHOLD = _float_env("LOWER_BAND_THRESHOLD", 0.06)
# Bir videoda bir marka bu kadar (veya daha fazla) kez görünürse otomatik ANA SPONSOR
# sayılır (+ köşe logosu sayılmaz) — şişik veriyi (ör. 4000 Predator) önler.
AUTO_SPONSOR_THRESHOLD = _int_env("AUTO_SPONSOR_THRESHOLD", 70)
# Shorts atlama: süresi 1..bu değer (sn) arası olan videolar Shorts sayılıp atlanır
SHORTS_MAX_DURATION = _int_env("SHORTS_MAX_DURATION", 60)
# Frame depolama üst sınırı (MB). Toplam frame boyutu bunu aşarsa en eski video
# klasörleri otomatik silinir (volume dolmasın). Yalnız kanıt kareleri saklandığı
# için bu sınıra ulaşmak zordur; yine de güvenlik için.
FRAME_STORAGE_CAP_MB = _int_env("FRAME_STORAGE_CAP_MB", 300)
# Süresi BİLİNMEYEN (şu an canlı) yayında lineer ffmpeg en fazla bu kadar saniye
# okur — yoksa canlı yayın sonsuza dek okunur, kuyruk tıkanır. Bitmiş yayınlar
# (süresi bilinen) bundan etkilenmez; tüm VOD paralel seek ile çıkarılır.
LIVE_SAMPLE_SECONDS = _int_env("LIVE_SAMPLE_SECONDS", 600)
# Uzun videolarda kare patlamasını önle: süreye göre aralık ayarlanır, en fazla
# bu kadar kare çıkarılır (2.5 saatlik yayın 1100 kare yerine ~bu kadar olur).
TARGET_SAMPLE_FRAMES = _int_env("TARGET_SAMPLE_FRAMES", 200)
# Gemini'ye gönderilecek MAKSİMUM kare (aday) sayısı — video ne kadar uzun olursa
# olsun. ÜCRETSİZ KATMAN GÜNLÜK İSTEK (RPD ~500) darboğazı için kritik: 60 kare /
# BATCH_SIZE(12) ≈ 5 istek/video → 30 video/gece ≈ 150 istek (kotanın altında).
# Ücretli katmanda büyük ver (ör. 400) → tam kapsama.
MAX_API_FRAMES = _int_env("MAX_API_FRAMES", 60)

# Spor yayınlarında REKLAM SAYILMAYACAK kulüp/lig/federasyon/milli takım adları.
# Forma/saha SPONSOR markaları (bahis, banka, telekom) bundan etkilenmez — onlar
# reklamdır. Bu liste yalnız kulüp KİMLİĞİ (arma/isim) için. UI'dan düzenlenebilir.
DEFAULT_SPORTS_IGNORE = [
    # Türkiye — Süper Lig ve büyükler
    "Fenerbahçe", "Galatasaray", "Beşiktaş", "Trabzonspor", "Başakşehir",
    "Adana Demirspor", "Konyaspor", "Antalyaspor", "Kayserispor", "Sivasspor",
    "Alanyaspor", "Gaziantep FK", "Kasımpaşa", "Hatayspor", "Samsunspor",
    "Rizespor", "Çaykur Rizespor", "Pendikspor", "Ankaragücü", "İstanbulspor",
    "Bodrumspor", "Eyüpspor", "Göztepe", "Kocaelispor", "Bursaspor",
    # Avrupa büyükleri
    "Real Madrid", "Barcelona", "Atletico Madrid", "Bayern", "Bayern Münih",
    "Borussia Dortmund", "Manchester City", "Manchester United", "Liverpool",
    "Arsenal", "Chelsea", "Tottenham", "Juventus", "Inter", "Milan", "Napoli",
    "PSG", "Paris Saint-Germain", "Ajax", "Benfica", "Porto",
    # Lig / turnuva / federasyon
    "UEFA", "FIFA", "TFF", "Süper Lig", "Trendyol Süper Lig", "Şampiyonlar Ligi",
    "Champions League", "Avrupa Ligi", "Europa League", "Konferans Ligi",
    "Premier League", "La Liga", "Bundesliga", "Serie A", "Ligue 1",
    "Dünya Kupası", "World Cup", "EURO", "Milli Takım", "A Milli Takım",
]

DEFAULT_CHANNELS = [
    "https://www.youtube.com/@343digital",
    "https://www.youtube.com/@eskiacikonline",
    "https://www.youtube.com/@HodriMeydan_TV",
    "https://www.youtube.com/@SportsDigitale",
    "https://www.youtube.com/@Neo_Spor",
    "https://www.youtube.com/@VOLEapp",
    "https://www.youtube.com/@sporontv",
    "https://www.youtube.com/@HTalksYoutube",
    "https://www.youtube.com/@SocratesDergi",
    "https://www.youtube.com/@nowsportr",
    "https://www.youtube.com/@yagosabuncuoglu",
]

_CONFIG_FILE = DATA_DIR / "config.json"

# ── Otomatik gece taraması (envanter canlı yayınları) varsayılanları ──
# Her `interval_min` dakikada BİR canlı yayın analiz edilir. 03:00–09:30 arası,
# 15 dk ile ≈26 yayın/gece → Gemini günlük kotasını (3.1-flash-lite ~500 RPD)
# zorlamaz. Hepsi UI Ayarlar'dan değiştirilebilir (config.json'a yazılır).
DEFAULT_AUTO_SCAN = {
    "enabled": True,
    "start": os.environ.get("AUTO_SCAN_START", "03:00"),     # HH:MM (yerel saat)
    "end": os.environ.get("AUTO_SCAN_END", "09:30"),         # HH:MM
    "interval_min": _int_env("AUTO_SCAN_INTERVAL_MIN", 15),  # analiz temposu (dk)
    "lookback_hours": _int_env("AUTO_SCAN_LOOKBACK_HOURS", 24),  # ilk keşif geriye-bakış
    "content_type": "live",                                  # sadece canlı yayın
    "nightly_cap": _int_env("AUTO_SCAN_NIGHTLY_CAP", 30),    # gecelik güvenlik üst sınırı
    "tz_offset": _int_env("AUTO_SCAN_TZ_OFFSET", 3),         # UTC ofseti (TR=+3) — saat hesabı buna göre
}


def _merge_auto_scan(saved):
    """Kayıtlı auto_scan üzerine default'ları bindirir (eksik alanlar tamamlanır)."""
    merged = dict(DEFAULT_AUTO_SCAN)
    if isinstance(saved, dict):
        for k in merged:
            if k in saved and saved[k] is not None:
                merged[k] = saved[k]
    return merged


def load_config():
    # Önce env var'ı taban olarak al
    cfg = {
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "channels": list(DEFAULT_CHANNELS),
        "auto_scan": dict(DEFAULT_AUTO_SCAN),
        "global_ignored_brands": list(DEFAULT_SPORTS_IGNORE),
    }
    # config.json varsa üzerine yazar — UI'dan yapılan değişiklik her zaman kazanır
    if _CONFIG_FILE.exists():
        try:
            saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            if saved.get("gemini_api_key"):
                cfg["gemini_api_key"] = saved["gemini_api_key"]
            if saved.get("channels"):
                cfg["channels"] = saved["channels"]
            cfg["auto_scan"] = _merge_auto_scan(saved.get("auto_scan"))
            if saved.get("global_ignored_brands") is not None:
                cfg["global_ignored_brands"] = saved["global_ignored_brands"]
        except Exception:
            pass
    return cfg


def save_config(cfg):
    _CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
