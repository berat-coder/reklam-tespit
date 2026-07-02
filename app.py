import os
import secrets
from datetime import timedelta
from flask import (
    Flask, send_from_directory, request, session, redirect, Response,
)
from config import BASE_DIR, DATA_DIR, FRAMES_DIR, FRAME_STORAGE_CAP_MB

# Frame'ler artık KALICI (volume). Açılışta toplam boyut cap'i aşılmışsa en eski
# video klasörlerini buda (volume dolmasın) — silmek yerine sınırla.
try:
    from services.storage import frame_maintenance
    frame_maintenance()   # eski kareleri (retention) + cap aşımını temizle → yer aç
except Exception as e:
    print(f"[BAKIM] Frame bakımı atlandı: {e}")

from models.database import init_db, verify_user, get_user_role
from routes.api import api_bp
from auth import APP_USERNAME, APP_PASSWORD, AUTH_ENABLED, is_admin, current_user
from flask import jsonify

app = Flask(__name__, static_folder=None)
# Oturum anahtarı: sabit olsun ki deploy/restart'ta çıkış yapılmasın
app.secret_key = (
    os.environ.get("SECRET_KEY")
    or (("rt-secret-" + APP_PASSWORD) if APP_PASSWORD else secrets.token_hex(16))
)
app.permanent_session_lifetime = timedelta(days=30)

app.register_blueprint(api_bp)
init_db()

# Gece otomatik canlı-yayın taraması zamanlayıcısı (web process'inde tek thread).
# RQ worker app.py'ı import etmediği için yalnız web'de doğar; --workers 1 → tek instance.
try:
    from services.scheduler import start_scheduler
    start_scheduler()
except Exception as _e:
    print(f"[OTO-TARAMA] başlatılamadı: {_e}")


_LOGIN_HTML = """<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Giriş — Reklam Tespit</title>
<style>
*{box-sizing:border-box} body{margin:0;background:#f6f7f9;color:#15171c;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border:1px solid #e7e9ed;border-radius:16px;padding:32px;width:320px;
 box-shadow:0 4px 12px rgba(16,24,40,.08),0 2px 4px rgba(16,24,40,.05)}
.logo{width:40px;height:40px;background:#15171c;color:#fff;border-radius:10px;display:flex;
 align-items:center;justify-content:center;font-size:18px;margin-bottom:16px}
h1{font-size:18px;margin:0 0 4px} p{color:#6b7280;font-size:13px;margin:0 0 20px}
label{display:block;font-size:12px;color:#6b7280;margin:12px 0 4px}
input{width:100%;background:#fff;border:1px solid #d8dce2;color:#15171c;
 padding:10px 12px;border-radius:8px;font-size:14px}
input:focus{outline:none;border-color:#15171c}
button{width:100%;margin-top:18px;background:#15171c;border:none;color:#fff;
 padding:11px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#000}
.err{color:#e5484d;font-size:12px;margin-top:12px;min-height:16px}
</style></head><body>
<form class="card" method="POST" action="/login">
  <div class="logo">▶</div>
  <h1>Reklam Tespit</h1>
  <p>Devam etmek için giriş yap</p>
  <label>Kullanıcı adı</label>
  <input name="username" autocomplete="username" autofocus>
  <label>Şifre</label>
  <input name="password" type="password" autocomplete="current-password">
  <button type="submit">Giriş Yap</button>
  <div class="err">{{ERROR}}</div>
</form></body></html>"""


# Yöneticiye özel GET uçları (kullanıcı yönetimi gibi okuma işlemleri de gizli).
_ADMIN_ONLY_GET_PREFIXES = ("/api/users",)


@app.before_request
def _require_login():
    if request.path in ("/login", "/logout"):
        return
    # Auth kapalıysa (local) herkes yönetici — geç.
    if not AUTH_ENABLED:
        return
    # Giriş yapılmamış
    if not session.get("logged_in"):
        if request.path.startswith("/api/"):
            return Response('{"error":"Giriş gerekli","auth_required":true}',
                            status=401, mimetype="application/json")
        return redirect("/login")
    # ── Rol zorlaması: yönetici değilse SADECE GÖRÜNTÜLEME (GET) ──
    if not is_admin():
        mutating = request.method not in ("GET", "HEAD", "OPTIONS")
        admin_only_get = any(request.path.startswith(p) for p in _ADMIN_ONLY_GET_PREFIXES)
        if (mutating or admin_only_get) and request.path.startswith("/api/"):
            return Response(
                '{"error":"Bu işlem için yönetici yetkisi gerekli","forbidden":true}',
                status=403, mimetype="application/json")
        if mutating and not request.path.startswith("/api/"):
            return Response("Yetki yok", status=403)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect("/")
    error = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        # Ana hesap (env) VEYA Ayarlar'dan oluşturulmuş DB kullanıcısı
        if u == APP_USERNAME and pw == APP_PASSWORD:
            session["logged_in"] = True
            session["username"] = u
            session["role"] = "admin"            # ana hesap her zaman yönetici
            session.permanent = True
            return redirect("/")
        if verify_user(u, pw):
            session["logged_in"] = True
            session["username"] = u
            session["role"] = get_user_role(u)   # DB kullanıcısının rolü
            session.permanent = True
            return redirect("/")
        error = "Hatalı kullanıcı adı veya şifre"
    return _LOGIN_HTML.replace("{{ERROR}}", error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/me")
def api_me():
    return jsonify(current_user())


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(BASE_DIR / "static", p)


@app.route("/frames/<video_id>/<filename>")
def frame_files(video_id, filename):
    return send_from_directory(FRAMES_DIR / video_id, filename)


if __name__ == "__main__":
    import threading
    import time

    def _open_browser():
        import webbrowser
        time.sleep(1.0)
        webbrowser.open("http://127.0.0.1:5001")

    print("\n" + "=" * 58)
    print("  YouTube Reklam Tespit v4")
    print("  Tarayıcı: http://127.0.0.1:5001")
    print("=" * 58 + "\n")
    if not os.environ.get("NO_BROWSER"):
        threading.Thread(target=_open_browser, daemon=True).start()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5001)),
        debug=False,
        threaded=True,
    )
