"""
İkinci-model görsel doğrulama sağlayıcıları (yalnız ücretsiz katmanlar).

Gemini'nin reklam bulduğu kareler, farklı bir görsel modele "bu gerçekten yayına
eklenmiş reklam mı, yoksa tesadüfi marka görünümü mü (forma sponsoru, masadaki
su şişesi, basın panosu)?" diye tekrar sorulur. Yanlış pozitifleri eler.

Tüm sağlayıcılar OpenAI chat-completions uyumlu API kullanır; anahtarı olan
ilk sağlayıcı seçilir (VERIFY_PROVIDER_ORDER). Hata birincil analizi ASLA
etkilemez — çağıran (_verify_video_core) her şeyi try/except içinde yapar.
"""

import os
import json
import time
import requests

from services.gemini import RateLimiter, parse_json_safe

VERIFY_RPM = int(os.environ.get("VERIFY_RPM", "10") or 10)
VERIFY_BATCH_SIZE = int(os.environ.get("VERIFY_BATCH_SIZE", "8") or 8)
VERIFY_DAILY_CAP = int(os.environ.get("VERIFY_DAILY_CAP", "180") or 180)
_PROVIDER_ORDER = [p.strip() for p in os.environ.get(
    "VERIFY_PROVIDER_ORDER", "openrouter,groq,mistral").split(",") if p.strip()]

_LIMITER = RateLimiter(VERIFY_RPM)

# ── OpenRouter: canlı ücretsiz görsel model seçimi ────────────────────────────
# Ücretsiz modeller OpenRouter'dan habersizce kaldırılıyor (2026-09-01:
# qwen2.5-vl-72b:free 404 dönmeye başladı ve doğrulama sessizce devre dışı
# kaldı). Sabit model adı yerine /models listesinden görsel destekli ücretsiz
# modeller çekilir (6 saat kv önbelleği), tercih sırasına dizilir; ölü model
# (404/402/410, "model" içeren 400) 24 saat karalisteye alınıp sıradakine
# geçilir. OPENROUTER_VERIFY_MODEL tanımlıysa her zaman İLK aday odur.

_OR_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OR_CACHE_KEY = "verify_or_models"      # {"ts": epoch, "ids": [...]}
_OR_BAD_KEY = "verify_or_bad"           # {"model_id": epoch_until}
_OR_CACHE_TTL = 6 * 3600
_OR_BAD_TTL = 24 * 3600
_OR_MAX_CANDIDATES = 6

_OR_PREFERRED = [   # marka/reklam ayrımında iyi bilinen aileler önce
    r"qwen.*vl", r"llama.*(vision|scout|maverick)", r"gemini.*flash",
    r"internvl", r"pixtral", r"gemma-3", r"phi.*(vision|multimodal)",
    r"omni", r"vl\b|-vl|vl-", r"vision",   # genel amaçlı VL ipuçları
]
# Amaca uygun olmayan uzmanlaşmış modeller (içerik güvenliği, moderasyon,
# koruma katmanı): 200 dönseler bile marka/reklam sorusuna anlamlı yanıt
# vermezler — parse hatasıyla sessizce batch kaybettirirler.
_OR_EXCLUDE = r"guard|safety|moderat"


def _kv():
    from models.database import kv_get, kv_set
    return kv_get, kv_set


def _or_fetch_free_vision_models(api_key):
    """OpenRouter /models → ücretsiz + görsel girdili model id'leri (tercih sıralı)."""
    import re as _re
    r = requests.get(_OR_MODELS_URL, timeout=30,
                     headers={"Authorization": f"Bearer {api_key}"})
    if r.status_code != 200:
        return []
    ids = []
    for m in (r.json().get("data") or []):
        mid = m.get("id") or ""
        arch = m.get("architecture") or {}
        mods = arch.get("input_modalities") or arch.get("modality") or ""
        has_image = "image" in (mods if isinstance(mods, str) else " ".join(mods))
        pricing = m.get("pricing") or {}
        free = mid.endswith(":free") or (
            str(pricing.get("prompt", "1")).rstrip("0.") == "" and
            str(pricing.get("completion", "1")).rstrip("0.") == "")
        if mid and has_image and free and not _re.search(_OR_EXCLUDE, mid.lower()):
            ids.append(mid)

    def _score(mid):
        low = mid.lower()
        for i, pat in enumerate(_OR_PREFERRED):
            if _re.search(pat, low):
                return i
        return len(_OR_PREFERRED)
    ids.sort(key=_score)   # sort kararlı — eşitlikte API sırası korunur
    return ids


def _or_model_ids(api_key):
    """Önbellekli model listesi; önbellek bayat/boşsa yeniden çekip kv'ye yazar."""
    kv_get, kv_set = _kv()
    cache = kv_get(_OR_CACHE_KEY, {}) or {}
    if cache.get("ids") and time.time() - float(cache.get("ts") or 0) < _OR_CACHE_TTL:
        return cache["ids"]
    try:
        ids = _or_fetch_free_vision_models(api_key)
    except Exception:
        ids = []
    if ids:
        kv_set(_OR_CACHE_KEY, {"ts": time.time(), "ids": ids})
        return ids
    return cache.get("ids") or []   # çekilemedi → eldeki bayat liste hiç yoktan iyi


def _or_mark_bad(model_id, reason=""):
    """Ölü modeli 24 saat karalisteye al (süresi geçmiş kayıtlar temizlenir)."""
    try:
        kv_get, kv_set = _kv()
        bad = kv_get(_OR_BAD_KEY, {}) or {}
        now = time.time()
        bad = {k: v for k, v in bad.items() if float(v) > now}
        bad[model_id] = now + _OR_BAD_TTL
        kv_set(_OR_BAD_KEY, bad)
    except Exception:
        pass
    print(f"[DOĞRULAMA] openrouter modeli karalisteye alındı ({model_id}): "
          f"{(reason or '')[:100]}")


def openrouter_candidates(api_key):
    """Denenecek model sırası: env override → tercih sıralı canlı liste,
    karaliste düşülmüş, en fazla _OR_MAX_CANDIDATES aday."""
    kv_get, _ = _kv()
    try:
        bad = kv_get(_OR_BAD_KEY, {}) or {}
    except Exception:
        bad = {}
    now = time.time()
    env_model = os.environ.get("OPENROUTER_VERIFY_MODEL", "").strip()
    ordered = ([env_model] if env_model else []) + [
        m for m in _or_model_ids(api_key) if m != env_model]
    live = [m for m in ordered if float(bad.get(m) or 0) <= now]
    return live[:_OR_MAX_CANDIDATES]


def _is_model_dead_err(err):
    """Model-seviyesi kalıcı hata mı (başka modele geçmek çözer)?"""
    if not err or not err.startswith("API hata"):
        return False
    code = err.split()[2].rstrip(":") if len(err.split()) > 2 else ""
    if code in ("404", "402", "410"):
        return True
    return code == "400" and "model" in err.lower()


class VisionProvider:
    """Ortak arayüz: analyze_frames([{index, b64}], prompt) → (parsed, err)."""

    name = "base"
    url = ""
    model = ""

    def __init__(self, api_key):
        self.api_key = api_key

    def analyze_frames(self, frames, prompt, max_attempts=3):
        content = [{"type": "text", "text": prompt}]
        for f in frames:
            content.append({"type": "text", "text": f"=== KARE {f['index']} ==="})
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + f["b64"]}})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": 300 * max(1, len(frames)),
        }
        for attempt in range(max_attempts):
            _LIMITER.wait()
            try:
                r = requests.post(self.url, json=payload, timeout=90, headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                })
                if r.status_code == 200:
                    try:
                        text = r.json()["choices"][0]["message"]["content"] or ""
                    except (KeyError, IndexError, TypeError):
                        return None, "yanıt biçimi beklenmedik"
                    parsed = parse_json_safe(text)
                    return parsed, None
                if r.status_code == 429:
                    low = (r.text or "").lower()
                    if "day" in low or "daily" in low:
                        return None, "QUOTA_DAILY"
                    time.sleep(15 * (attempt + 1))
                    continue
                if r.status_code in (500, 502, 503, 504):
                    time.sleep(10 * (attempt + 1))
                    continue
                return None, f"API hata {r.status_code}: {r.text[:120]}"
            except Exception as e:
                if attempt < max_attempts - 1:
                    time.sleep(5)
                    continue
                return None, f"Bağlantı: {str(e)[:60]}"
        return None, "Sürekli yoğun"


class OpenRouterProvider(VisionProvider):
    """Model adı sabit değil: her çağrıda canlı aday listesi denenir, ölü model
    karalisteye alınıp sıradakine geçilir — 'yeter ki çalışsın'."""
    name = "openrouter"
    url = "https://openrouter.ai/api/v1/chat/completions"
    model = ""   # analyze_frames aday üzerinden set eder

    def analyze_frames(self, frames, prompt, max_attempts=3):
        candidates = openrouter_candidates(self.api_key)
        if not candidates:
            return None, "OpenRouter'da kullanılabilir ücretsiz görsel model yok"
        last_err = None
        for model_id in candidates:
            self.model = model_id
            parsed, err = super().analyze_frames(frames, prompt, max_attempts)
            if err and _is_model_dead_err(err):
                _or_mark_bad(model_id, err)
                last_err = err
                continue   # sıradaki adayla devam
            return parsed, err
        return None, last_err or "tüm openrouter adayları düştü"


class GroqProvider(VisionProvider):
    name = "groq"
    url = "https://api.groq.com/openai/v1/chat/completions"
    model = os.environ.get("GROQ_VERIFY_MODEL",
                           "meta-llama/llama-4-scout-17b-16e-instruct")


class MistralProvider(VisionProvider):
    name = "mistral"
    url = "https://api.mistral.ai/v1/chat/completions"
    model = os.environ.get("MISTRAL_VERIFY_MODEL", "pixtral-12b-2409")


_PROVIDERS = {
    "openrouter": (OpenRouterProvider, "OPENROUTER_API_KEY", "openrouter_api_key"),
    "groq": (GroqProvider, "GROQ_API_KEY", "groq_api_key"),
    "mistral": (MistralProvider, "MISTRAL_API_KEY", "mistral_api_key"),
}


def _provider_key(env_name, cfg_name, cfg=None):
    """Anahtar öncelik sırası: config.json (yalnız web'de var) → env → Postgres app_kv.
    app_kv kritik: UI'dan girilen anahtar oraya da yazılır ve worker (ayrı servis,
    ayrı disk) anahtarı ancak oradan görebilir."""
    if cfg and cfg.get(cfg_name):
        return cfg[cfg_name]
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    try:
        from models.database import kv_get
        return ((kv_get("verify_keys", {}) or {}).get(cfg_name) or "").strip()
    except Exception:
        return ""


def _tr_day():
    from datetime import datetime, timedelta
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")


_QUOTA_KEY = "verify_provider_quota"    # {"openrouter": "2026-09-01", ...}


def mark_provider_quota(name):
    """Sağlayıcının GÜNLÜK (hesap seviyesi) kotası doldu — bugünlük atla.
    Model değiştirmek hesap kotasını çözmez; ertesi TR günü otomatik açılır."""
    try:
        kv_get, kv_set = _kv()
        st = kv_get(_QUOTA_KEY, {}) or {}
        st[name] = _tr_day()
        kv_set(_QUOTA_KEY, st)
    except Exception:
        pass


def _provider_quota_exhausted(name):
    try:
        kv_get, _ = _kv()
        return (kv_get(_QUOTA_KEY, {}) or {}).get(name) == _tr_day()
    except Exception:
        return False


def get_verifier(cfg=None):
    """Anahtarı tanımlı ve günlük kotası dolmamış ilk sağlayıcı, yoksa None
    (doğrulama sessizce atlanır)."""
    if str(os.environ.get("VERIFY_ENABLED", "1")).strip() in ("0", "false", "no"):
        return None
    for name in _PROVIDER_ORDER:
        entry = _PROVIDERS.get(name)
        if not entry:
            continue
        if _provider_quota_exhausted(name):
            continue
        cls, env_name, cfg_name = entry
        key = _provider_key(env_name, cfg_name, cfg)
        if key:
            return cls(key)
    return None


# ── Günlük istek bütçesi (app_kv üzerinde gün-anahtarlı sayaç) ─────────────────

def _budget_state():
    from models.database import kv_get
    return kv_get("verify_daily", {}) or {}


def verify_budget_left():
    from datetime import datetime, timedelta
    day = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")  # TR günü
    st = _budget_state()
    used = st.get("count", 0) if st.get("day") == day else 0
    return max(0, VERIFY_DAILY_CAP - used)


def verify_budget_spend(n=1):
    from datetime import datetime, timedelta
    from models.database import kv_set
    day = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d")
    st = _budget_state()
    used = st.get("count", 0) if st.get("day") == day else 0
    kv_set("verify_daily", {"day": day, "count": used + n})
