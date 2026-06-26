import os
import shutil
import secrets
from datetime import timedelta
from flask import (
    Flask, send_from_directory, request, session, redirect, Response,
)
from config import BASE_DIR, DATA_DIR, FRAMES_DIR

# Volume'u şişiren eski frame'leri temizle (artık efemeral diskte tutuluyor).
# init_db'den ÖNCE: dolu disk SQLite'ı patlatmasın diye önce yer aç.
try:
    _old_frames = DATA_DIR / "frames"
    if _old_frames.exists() and _old_frames.resolve() != FRAMES_DIR.resolve():
        shutil.rmtree(_old_frames, ignore_errors=True)
        print(f"[BAKIM] Eski frame dizini temizlendi: {_old_frames}")
except Exception as e:
    print(f"[BAKIM] Frame temizleme atlandı: {e}")

from models.database import init_db, verify_user
from routes.api import api_bp

app = Flask(__name__, static_folder=None)

# ── Kimlik doğrulama (basit oturum) ──
# APP_PASSWORD ayarlıysa giriş zorunlu olur; yoksa (local) kapalı kalır.
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)
# Oturum anahtarı: sabit olsun ki deploy/restart'ta çıkış yapılmasın
app.secret_key = (
    os.environ.get("SECRET_KEY")
    or (("rt-secret-" + APP_PASSWORD) if APP_PASSWORD else secrets.token_hex(16))
)
app.permanent_session_lifetime = timedelta(days=30)

app.register_blueprint(api_bp)
init_db()


_LOGIN_HTML = """<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Giriş — Reklam Tespit</title>
<style>
*{box-sizing:border-box} body{margin:0;background:#0a0a0f;color:#f0f0f6;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#14141d;border:1px solid #2e2e42;border-radius:14px;padding:32px;width:320px}
.logo{width:40px;height:40px;background:#ff4444;border-radius:9px;display:flex;
 align-items:center;justify-content:center;font-size:18px;margin-bottom:16px}
h1{font-size:18px;margin:0 0 4px} p{color:#8a8aa0;font-size:13px;margin:0 0 20px}
label{display:block;font-size:12px;color:#8a8aa0;margin:12px 0 4px}
input{width:100%;background:#0a0a0f;border:1px solid #2e2e42;color:#f0f0f6;
 padding:10px 12px;border-radius:8px;font-size:14px}
button{width:100%;margin-top:18px;background:#ff4444;border:none;color:#fff;
 padding:11px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.err{color:#ff4466;font-size:12px;margin-top:12px;min-height:16px}
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


@app.before_request
def _require_login():
    if not AUTH_ENABLED or session.get("logged_in"):
        return
    if request.path == "/login":
        return
    if request.path.startswith("/api/"):
        return Response('{"error":"Giriş gerekli","auth_required":true}',
                        status=401, mimetype="application/json")
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect("/")
    error = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        # Ana hesap (env) VEYA Ayarlar'dan oluşturulmuş DB kullanıcısı
        if (u == APP_USERNAME and pw == APP_PASSWORD) or verify_user(u, pw):
            session["logged_in"] = True
            session["username"] = u
            session.permanent = True
            return redirect("/")
        error = "Hatalı kullanıcı adı veya şifre"
    return _LOGIN_HTML.replace("{{ERROR}}", error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


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
