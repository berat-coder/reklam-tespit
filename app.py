import os
from flask import Flask, send_from_directory
from config import BASE_DIR, FRAMES_DIR
from models.database import init_db
from routes.api import api_bp

app = Flask(__name__, static_folder=None)
app.register_blueprint(api_bp)

init_db()


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
