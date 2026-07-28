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
    name = "openrouter"
    url = "https://openrouter.ai/api/v1/chat/completions"
    model = os.environ.get("OPENROUTER_VERIFY_MODEL",
                           "qwen/qwen2.5-vl-72b-instruct:free")


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
    """config.json (UI'dan girilen) env'i ezer — gemini_api_key ile aynı düzen."""
    if cfg and cfg.get(cfg_name):
        return cfg[cfg_name]
    return os.environ.get(env_name, "").strip()


def get_verifier(cfg=None):
    """Anahtarı tanımlı ilk sağlayıcı, yoksa None (doğrulama sessizce atlanır)."""
    if str(os.environ.get("VERIFY_ENABLED", "1")).strip() in ("0", "false", "no"):
        return None
    for name in _PROVIDER_ORDER:
        entry = _PROVIDERS.get(name)
        if not entry:
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
