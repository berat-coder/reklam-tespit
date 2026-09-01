"""
Dogrulama modeli otomatik secimi testleri — AG YOK, GECICI DB.

Neden kritik: OpenRouter ucretsiz modelleri habersizce kaldiriliyor
(2026-09-01: qwen2.5-vl-72b:free 404 dondu ve dogrulama SESSIZCE devre disi
kaldi — loglar "batch hatasi (atlandi)" ile doldu, kimse fark etmedi).
Artik model adi sabit degil: /models listesinden ucretsiz+gorsel modeller
cekilir, olu model karalisteye alinip siradakine gecilir, gunluk hesap
kotasi dolan saglayici o gun atlanir.

Calistirma:  ./.venv/bin/python tests/test_verify_models.py
"""
import sys, os, tempfile, time, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = ""
_TMPDIR = tempfile.mkdtemp(prefix="rt-verify-")
os.environ["DATA_DIR"] = _TMPDIR
os.environ["AUTO_SCAN_ENABLED"] = "0"
os.environ.pop("OPENROUTER_VERIFY_MODEL", None)

import app as _a                                                   # noqa: E402,F401
from services import vision_providers as VP                        # noqa: E402
from models.database import kv_get, kv_set                         # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, f"BASARISIZ: {label}"
    PASS += 1
    print(f"  ✓ {label}")


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _models_payload():
    return {"data": [
        {"id": "some/text-only:free",
         "architecture": {"input_modalities": ["text"]}, "pricing": {}},
        {"id": "acme/paid-vl",
         "architecture": {"input_modalities": ["text", "image"]},
         "pricing": {"prompt": "0.002", "completion": "0.01"}},
        {"id": "acme/other-vl:free",
         "architecture": {"input_modalities": ["text", "image"]}, "pricing": {}},
        {"id": "qwen/qwen3-vl-30b:free",
         "architecture": {"modality": "text+image->text"},
         "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "meta-llama/llama-4-scout:free",
         "architecture": {"input_modalities": ["text", "image"]}, "pricing": {}},
        {"id": "vendor/content-safety-vl:free",
         "architecture": {"input_modalities": ["text", "image"]}, "pricing": {}},
    ]}


# ── 1. /models ayiklama: ucretsiz + gorsel filtre, tercih sirasi ─────────────
print("1) /models ayiklama ve tercih sirasi")
_fetch_calls = [0]


def _fake_get(url, **kw):
    _fetch_calls[0] += 1
    return FakeResp(200, _models_payload())


VP.requests.get = _fake_get
ids = VP._or_fetch_free_vision_models("k")
ok("some/text-only:free" not in ids, "gorsel girdisiz model elendi")
ok("acme/paid-vl" not in ids, "ucretli model elendi")
ok(ids[0] == "qwen/qwen3-vl-30b:free", "qwen-vl tercihte ilk sirada")
ok(ids[1] == "meta-llama/llama-4-scout:free", "llama-scout ikinci sirada")
ok("vendor/content-safety-vl:free" not in ids, "guvenlik/moderasyon modeli elendi")
ok(ids[-1] == "acme/other-vl:free", "bilinen aile disindaki VL sona atildi")

# ── 2. Onbellek: ikinci cagri /models'e gitmez ───────────────────────────────
print("2) kv onbellegi")
kv_set(VP._OR_CACHE_KEY, {})
_fetch_calls[0] = 0
first = VP._or_model_ids("k")
second = VP._or_model_ids("k")
ok(first == second and _fetch_calls[0] == 1, "ikinci cagri onbellekten geldi")
kv_set(VP._OR_CACHE_KEY, {"ts": time.time() - VP._OR_CACHE_TTL - 1, "ids": ["eski/model"]})
VP._or_model_ids("k")
ok(_fetch_calls[0] == 2, "bayat onbellek yeniden cekildi")


def _fail_get(url, **kw):
    raise RuntimeError("ag yok")


VP.requests.get = _fail_get
kv_set(VP._OR_CACHE_KEY, {"ts": time.time() - VP._OR_CACHE_TTL - 1, "ids": ["bayat/model:free"]})
ok(VP._or_model_ids("k") == ["bayat/model:free"], "cekim dusunce bayat liste kullanildi")
VP.requests.get = _fake_get
kv_set(VP._OR_CACHE_KEY, {})

# ── 3. Karaliste ─────────────────────────────────────────────────────────────
print("3) olu model karalistesi")
kv_set(VP._OR_BAD_KEY, {})
VP._or_mark_bad("qwen/qwen3-vl-30b:free", "API hata 404: kalkti")
cands = VP.openrouter_candidates("k")
ok("qwen/qwen3-vl-30b:free" not in cands, "karalistedeki model adaylardan cikti")
ok(cands[0] == "meta-llama/llama-4-scout:free", "siradaki tercih one gecti")
kv_set(VP._OR_BAD_KEY, {"meta-llama/llama-4-scout:free": time.time() - 5})
cands = VP.openrouter_candidates("k")
ok("meta-llama/llama-4-scout:free" in cands, "suresi dolan karaliste kaydi acildi")

# ── 4. Env override ilk aday ─────────────────────────────────────────────────
print("4) OPENROUTER_VERIFY_MODEL onceligi")
os.environ["OPENROUTER_VERIFY_MODEL"] = "ozel/model"
ok(VP.openrouter_candidates("k")[0] == "ozel/model", "env modeli ilk aday")
os.environ.pop("OPENROUTER_VERIFY_MODEL", None)

# ── 5. Olu model hatasi siniflandirmasi ──────────────────────────────────────
print("5) _is_model_dead_err")
ok(VP._is_model_dead_err("API hata 404: kalkti"), "404 olu")
ok(VP._is_model_dead_err("API hata 402: odeme"), "402 olu")
ok(VP._is_model_dead_err("API hata 400: bilinmeyen model"), "400+model olu")
ok(not VP._is_model_dead_err("API hata 400: gecersiz istek"), "400 modelsiz olu degil")
ok(not VP._is_model_dead_err("API hata 500: sunucu"), "500 olu degil (retry base'de)")
ok(not VP._is_model_dead_err("QUOTA_DAILY"), "kota olu-model degil")
ok(not VP._is_model_dead_err(None), "None olu degil")

# ── 6. Failover: olu model → siradaki adayla ayni cagri icinde devam ─────────
print("6) analyze_frames failover")
kv_set(VP._OR_BAD_KEY, {})
kv_set(VP._OR_CACHE_KEY, {})
VP._LIMITER.wait = lambda: None
_posted = []


def _fake_post(url, json=None, **kw):
    _posted.append(json["model"])
    if json["model"] == "qwen/qwen3-vl-30b:free":
        return FakeResp(404, {"error": {"message": "model kalkti"}})
    return FakeResp(200, {"choices": [{"message": {
        "content": '[{"frame": 1, "karar": "reklam", "neden": "test"}]'}}]})


VP.requests.post = _fake_post
prov = VP.OpenRouterProvider("k")
parsed, err = prov.analyze_frames([{"index": 1, "b64": "x"}], "p")
ok(err is None and parsed and parsed[0]["karar"] == "reklam", "ikinci adaydan sonuc geldi")
ok(_posted == ["qwen/qwen3-vl-30b:free", "meta-llama/llama-4-scout:free"],
   "once olu model denendi, sonra siradaki")
ok(prov.model == "meta-llama/llama-4-scout:free", "calisan model kayitli (notlar icin)")
bad = kv_get(VP._OR_BAD_KEY, {})
ok("qwen/qwen3-vl-30b:free" in bad, "olu model karalisteye yazildi")

# ── 7. Saglayici gunluk kotasi ───────────────────────────────────────────────
print("7) saglayici kota atlamasi")
kv_set(VP._QUOTA_KEY, {})
os.environ["OPENROUTER_API_KEY"] = "or-key"
os.environ["GROQ_API_KEY"] = "groq-key"
v = VP.get_verifier()
ok(v is not None and v.name == "openrouter", "kota yokken openrouter secildi")
VP.mark_provider_quota("openrouter")
v = VP.get_verifier()
ok(v is not None and v.name == "groq", "kotasi dolan openrouter atlandi → groq")
kv_set(VP._QUOTA_KEY, {"openrouter": "2000-01-01"})
v = VP.get_verifier()
ok(v.name == "openrouter", "eski gunun kotasi bugunu etkilemedi")

print(f"\nTUM TESTLER GECTI ({PASS} kontrol)")
