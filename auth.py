"""
Kimlik & rol yardımcıları (app.py ve routes/api.py ortak kullanır).

İki rol:
  • yönetici (admin)     → her şeyi değiştirebilir (tüm yazma işlemleri)
  • kullanıcı (viewer)   → sadece görüntüler (yalnız GET); hiçbir şeyi değiştiremez

Yönetici kim olur?
  • Ana hesap (env APP_USERNAME) HER ZAMAN yöneticidir (eski oturumlar bile),
  • veya DB kullanıcısının rolü 'admin' ise.
Geri kalan giriş yapmış herkes 'kullanıcı' (sadece görüntüleme) sayılır.

Güvenlik: asıl sınır sunucudadır (app.before_request). UI'da buton gizlemek
yalnızca görsel kolaylıktır.
"""

import os
from flask import session

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)


def is_admin():
    # Auth kapalıysa (local geliştirme, APP_PASSWORD yok) herkes yöneticidir.
    if not AUTH_ENABLED:
        return True
    if session.get("username") == APP_USERNAME:
        return True
    return session.get("role") == "admin"


def current_user():
    return {
        "username": session.get("username") or (None if AUTH_ENABLED else "local"),
        "role": "admin" if is_admin() else "user",
        "is_admin": is_admin(),
        "auth_enabled": AUTH_ENABLED,
    }
