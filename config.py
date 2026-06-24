import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
FRAMES_DIR = BASE_DIR / "frames"
FRAMES_DIR.mkdir(exist_ok=True)

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

_CONFIG_FILE = BASE_DIR / "config.json"


def load_config():
    # Önce env var'ı taban olarak al
    cfg = {
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        "channels": list(DEFAULT_CHANNELS),
    }
    # config.json varsa üzerine yazar — UI'dan yapılan değişiklik her zaman kazanır
    if _CONFIG_FILE.exists():
        try:
            saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            if saved.get("gemini_api_key"):
                cfg["gemini_api_key"] = saved["gemini_api_key"]
            if saved.get("channels"):
                cfg["channels"] = saved["channels"]
        except Exception:
            pass
    return cfg


def save_config(cfg):
    _CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
