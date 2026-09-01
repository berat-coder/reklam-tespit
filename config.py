import os
import json
from pathlib import Path

# .env yükleme — python-dotenv yoksa (ör. bazı yerel python'lar) sessizce atla.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

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
# Gemini'ye gönderilen frame genişliği (px).
# 640 iken köşe logolarının MARKA YAZISI okunmuyordu: 640x360 karede logo alanı
# 180x64 px kalıyor, marka adı ~8 px yüksekliğinde bir lekeye dönüşüyor ve model
# tahmin yürütüyordu (ör. "Migros Hemen" → "n11"). 1280'de aynı yazı net okunur.
# MALİYET: Gemini görüntüyü 768x768 karolara böler; hem 960 hem 1280 genişlik
# 2 karo eder — yani 1280, 960 ile AYNI token'a çok daha okunur görüntü verir.
# İstek SAYISI değişmez (RPD/RPM darboğazı etkilenmez), yalnız istek başına
# token ~2 katına çıkar ki ücretsiz katmanda darboğaz token değil istek sayısıdır.
FRAME_WIDTH = _int_env("FRAME_WIDTH", 1280)
# İndirilecek KAYNAK akışın en düşük yüksekliği. Kritik: 480p kaynağı 1280'e
# büyütmek işe yaramaz, olmayan detay geri gelmez — köşe logosunun marka yazısı
# yine okunmaz. Ölçtük: aynı karede 480p ve 720p kaynakta "MiGROS" okunamayan
# bir leke, 1080p kaynakta net (dosya yalnız %8 büyük). 1920→1280 küçültme,
# 1280 native'den daha keskin metin verir.
SOURCE_MIN_HEIGHT = _int_env("SOURCE_MIN_HEIGHT", 1080)
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
# Frame depolama üst sınırı (MB). Tüm kareler saklanır; toplam boyut bunu aşarsa
# EN ESKİ video klasörleri otomatik silinir (volume dolmasın). 500MB volume için
# 300 → ~150MB gerçek boşluk (data.db/config/WAL için pay); onlarca video görüntülenir.
FRAME_STORAGE_CAP_MB = _int_env("FRAME_STORAGE_CAP_MB", 300)
# Kare saklama süresi (gün). Analizden bu kadar gün sonra videonun KARELERİ
# (görselleri) otomatik silinir → yer açılır. RAPOR/VERİ ASLA silinmez (DB'de
# kalır). Ayarlar'dan açılıp kapatılır. 0 = kapalı (sadece boyut cap'i geçerli).
DEFAULT_FRAME_RETENTION = {
    "enabled": True,
    "days": _int_env("FRAME_RETENTION_DAYS", 2),
}
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

# ── Kare çıkarma (seek) performans ayarları ──
# Paralel ffmpeg seek işçisi sayısı. Hızlı (keyframe) modda seek'ler bağlantı
# gecikmesiyle sınırlı olduğundan 10 güvenli; webshare proxy eşzamanlılık
# limitine takılırsanız düşürün.
FRAME_SEEK_WORKERS = _int_env("FRAME_SEEK_WORKERS", 10)
# Kare başına ffmpeg süreç zaman aşımı (sn). Hızlı modda tek keyframe
# indirildiği için 40 bol; eski değer 90 takılan seek'lerin slotu 1.5 dk
# işgal etmesine yol açıyordu.
FRAME_SEEK_TIMEOUT = _int_env("FRAME_SEEK_TIMEOUT", 40)
# 1 = keyframe seek (-noaccurate_seek -skip_frame nokey): decode maliyeti ~sıfır,
# indirme keyframe'de durur; kare zamanı istenen t'den en fazla ~5 sn (keyint)
# erken olabilir — örnekleme aralığı ≥8 sn olduğundan kabul edilebilir.
# 0 = eski hassas mod (kill-switch).
FRAME_SEEK_FAST = _int_env("FRAME_SEEK_FAST", 1)
# ffmpeg -rw_timeout (sn): ağ okuması bu kadar süre ilerlemezse süreç kendini
# keser — takılan bağlantı işçi slotunu bekletmez.
FRAME_SEEK_RW_TIMEOUT_SEC = _int_env("FRAME_SEEK_RW_TIMEOUT_SEC", 15)
# Video başına (marka|tür) çifti en fazla bu kadar karede kanıt olarak saklanır.
# Eskiden 3'tü; 2. model doğrulaması geldiği için 6'ya çıkarıldı — doğrulayıcının
# inceleyebileceği daha çok kanıt kalır, gürültüyü zaten doğrulayıcı eler.
BRAND_TUR_FRAME_CAP = _int_env("BRAND_TUR_FRAME_CAP", 6)
# OpenCV yedek kare çıkarımı için TOPLAM süre sınırı (sn).
# Neden var: ffmpeg 0 kare döndürdüğünde kod OpenCV yedeğine düşüyor ve
# cv2.VideoCapture canlı bir HLS akışına zaman aşımı olmadan bağlanmaya
# çalışıp SÜRESİZ bekliyordu. IP bloğu yüzünden ffmpeg sürekli 0 döndürünce
# her iş buraya düşüp TEK işçi slotunu sonsuza dek kilitliyordu → kuyruk
# tıkanıyor, hiçbir tarama bitmiyordu. Bu sınır kilitlenmeyi imkânsız kılar.
OPENCV_FALLBACK_TIMEOUT = _int_env("OPENCV_FALLBACK_TIMEOUT", 120)

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

# Reklam SAYILMAYACAK yerleşimler (placement). UI'dan düzenlenebilir.
#  • Forma        — oyuncunun GİYDİĞİ forma sponsoru
#  • Basın Panosu — kulübün basın toplantısı backdrop'u / medya duvarı / stat
#                   tabelası: kulübün sponsoru, yayının reklamı değil
#  • Satış Kanalı — pazaryeri/kargo/banka ("Trendyol'da satılır"): asıl
#                   reklamveren ürünün markası, pazaryeri değil
#  • Ürün Markası — başka bir markanın reklamında görünen ürün markası
#                   (BOYNER reklamındaki kıyafet markası): reklamveren o değil
# Overlay/alt bant/tam ekran/ürün yerleştirme/LED ve KANALIN stüdyo dekoru sayılır.
DEFAULT_EXCLUDED_PLACEMENTS = ["Forma", "Basın Panosu", "Satış Kanalı",
                               "Ürün Markası"]

# Sayıma girmek için gereken en düşük güven: "Yüksek" | "Orta" | "Düşük".
# Gemini'nin "Düşük" güvenle yazdığı tahminler sayılmaz (kanıtta görünür).
DEFAULT_MIN_CONFIDENCE = os.environ.get("MIN_CONFIDENCE", "Orta")

# Tahmini medya değeri (EMV): saniye başına TL. Kanal bazında Ayarlar'dan
# değiştirilebilir; burada global varsayılan. EMV = süre × ücret × belirginlik.
DEFAULT_EMV_RATE = _float_env("EMV_RATE", 12.0)

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

# ── Otomatik tarama (7/24) varsayılanları ──
# Zamanlayıcı SÜREKLİ çalışır: start–end arası "yoğun pencere"de her
# `interval_min` dk, pencere dışında her `day_interval_min` dk bir tick atar.
# Her tick'te (kuyruk boşsa) BİR canlı yayın analize gönderilir; `daily_cap`
# günlük toplam üst sınırdır (Gemini free ~500 RPD ÷ ~6 çağrı/video ≈ 83 tavan,
# 70 manuel işlere pay bırakır). Hepsi UI Ayarlar'dan değiştirilebilir.
DEFAULT_AUTO_SCAN = {
    "enabled": True,
    "start": os.environ.get("AUTO_SCAN_START", "03:00"),     # HH:MM (yerel saat) — yoğun pencere başı
    "end": os.environ.get("AUTO_SCAN_END", "09:30"),         # HH:MM — yoğun pencere sonu
    "interval_min": _int_env("AUTO_SCAN_INTERVAL_MIN", 15),  # yoğun pencere temposu (dk)
    "day_interval_min": _int_env("AUTO_SCAN_DAY_INTERVAL_MIN", 30),  # pencere dışı tempo (dk)
    "lookback_hours": _int_env("AUTO_SCAN_LOOKBACK_HOURS", 24),  # ilk keşif geriye-bakış
    "content_type": "all",                                   # canlı yayın + sıradan video
    "daily_cap": _int_env("AUTO_SCAN_DAILY_CAP", 70),        # günlük analiz üst sınırı
    "tz_offset": _int_env("AUTO_SCAN_TZ_OFFSET", 3),         # UTC ofseti (TR=+3) — saat hesabı buna göre
    "live_recheck_min": _int_env("LIVE_RECHECK_MIN", 45),    # canlı yayın "bitti mi" kontrol aralığı (dk)
    "live_wait_ttl_hours": _int_env("LIVE_WAIT_TTL_HOURS", 12),  # 7/24 yayın emniyeti: bu kadar saat sonra vazgeç
}


# Ayar alt sınırları. HEM yazma (POST /api/auto-scan/settings) HEM OKUMA
# yolunda uygulanır. Yalnız yazmada uygulamak yetmiyordu: /data/config.json'a
# daha önce kaydedilmiş interval_min=1 değeri okuma yolunda hiç sıkıştırılmadığı
# için üretimde zamanlayıcı 69 SANİYEDE BİR atıyordu (ölçüldü: aynı kanal için
# 23 ardışık aralığın hepsi ~69 sn; ayar 15 dk sanılıyordu). 13 kanal × saatte
# ~52 tick = saatte ~2.000 yt-dlp isteği.
AUTO_SCAN_MIN = {
    "interval_min": 5, "day_interval_min": 5, "live_recheck_min": 5,
    "lookback_hours": 1, "daily_cap": 1, "live_wait_ttl_hours": 1,
}


def clamp_auto_scan(cur):
    """Alt sınırları uygula (yerinde değiştirir ve döndürür)."""
    for k, low in AUTO_SCAN_MIN.items():
        if k in cur:
            try:
                cur[k] = max(low, int(cur[k]))
            except (TypeError, ValueError):
                cur[k] = DEFAULT_AUTO_SCAN.get(k, low)
    return cur


def _merge_auto_scan(saved):
    """Kayıtlı auto_scan üzerine default'ları bindirir (eksik alanlar tamamlanır).
    Eski kurulumlardaki `nightly_cap` → `daily_cap` migrasyonu burada yapılır."""
    merged = dict(DEFAULT_AUTO_SCAN)
    if isinstance(saved, dict):
        for k in merged:
            if k in saved and saved[k] is not None:
                merged[k] = saved[k]
        if "daily_cap" not in saved and saved.get("nightly_cap") is not None:
            # Gecelik 30'luk tavan 7/24 çalışmada çok düşük kalır — en az default'a çek
            merged["daily_cap"] = max(int(saved["nightly_cap"]), DEFAULT_AUTO_SCAN["daily_cap"])
        # ESKİ VARSAYILANIN MİRASI: content_type "live" hiçbir zaman bilinçli bir
        # tercih olmadı — zamanlayıcı bu alanı zaten hiç okumuyordu, yani kullanıcı
        # bunu bir ayar olarak deneyimlemedi. Sonuç: otomatik sistem sıradan
        # videoları HİÇ taramadı (ölçüm: son 24 saatte yayınlanan 9 videonun 8'i
        # sistemde yok). "all"a taşınıyor; kullanıcı arayüzden geri alabilir.
        if saved.get("content_type") == "live":
            merged["content_type"] = "all"
    return clamp_auto_scan(merged)


def load_config():
    # Önce env var'ı taban olarak al
    cfg = {
        "gemini_api_key": os.environ.get("GEMINI_API_KEY", ""),
        # 2. model doğrulama sağlayıcı anahtarları (config.json env'i ezer)
        "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", ""),
        "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
        "mistral_api_key": os.environ.get("MISTRAL_API_KEY", ""),
        "channels": list(DEFAULT_CHANNELS),
        "auto_scan": dict(DEFAULT_AUTO_SCAN),
        "global_ignored_brands": list(DEFAULT_SPORTS_IGNORE),
        "excluded_placements": list(DEFAULT_EXCLUDED_PLACEMENTS),
        "min_confidence": DEFAULT_MIN_CONFIDENCE,
        "emv_rate": DEFAULT_EMV_RATE,
        "frame_retention": dict(DEFAULT_FRAME_RETENTION),
    }
    # config.json varsa üzerine yazar — UI'dan yapılan değişiklik her zaman kazanır
    if _CONFIG_FILE.exists():
        try:
            saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            if saved.get("gemini_api_key"):
                cfg["gemini_api_key"] = saved["gemini_api_key"]
            for k in ("openrouter_api_key", "groq_api_key", "mistral_api_key"):
                if saved.get(k):
                    cfg[k] = saved[k]
            if saved.get("channels"):
                cfg["channels"] = saved["channels"]
            cfg["auto_scan"] = _merge_auto_scan(saved.get("auto_scan"))
            if saved.get("global_ignored_brands") is not None:
                cfg["global_ignored_brands"] = saved["global_ignored_brands"]
            if saved.get("excluded_placements") is not None:
                # Kayıtlı liste kazanır AMA sonradan eklenen yeni yerleşim
                # türleri (Basın Panosu, Satış Kanalı, Ürün Markası…) eski
                # kurulumlarda listede olmadığı için sessizce sayılmaya devam
                # ederdi. Kullanıcının hiç görmediği yeni türler bir kez eklenir;
                # sonradan Ayarlar'dan çıkarılabilir.
                seen = saved.get("known_placements") or []
                merged = list(saved["excluded_placements"])
                for p in DEFAULT_EXCLUDED_PLACEMENTS:
                    if p not in merged and p not in seen:
                        merged.append(p)
                cfg["excluded_placements"] = merged
            if saved.get("min_confidence"):
                cfg["min_confidence"] = saved["min_confidence"]
            if isinstance(saved.get("frame_retention"), dict):
                fr = dict(DEFAULT_FRAME_RETENTION)
                fr.update({k: saved["frame_retention"][k]
                           for k in ("enabled", "days")
                           if k in saved["frame_retention"]})
                cfg["frame_retention"] = fr
        except Exception:
            pass
    return cfg


def save_config(cfg):
    # Kullanıcının GÖRDÜĞÜ yerleşim türlerini damgala: böylece bir türü bilerek
    # listeden çıkardığında geri eklenmez, ama ileride eklenen YENİ türler bir
    # kez otomatik devreye girer (bkz. load_config).
    if cfg.get("excluded_placements") is not None:
        cfg = dict(cfg)
        cfg["known_placements"] = sorted(
            set(cfg.get("known_placements") or []) | set(DEFAULT_EXCLUDED_PLACEMENTS))
    _CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
