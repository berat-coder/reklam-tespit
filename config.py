import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
FRAMES_DIR = BASE_DIR / "frames"
FRAMES_DIR.mkdir(exist_ok=True)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)

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
    cfg = {"gemini_api_key": "", "channels": list(DEFAULT_CHANNELS)}
    if _CONFIG_FILE.exists():
        try:
            saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            if saved.get("gemini_api_key"):
                cfg["gemini_api_key"] = saved["gemini_api_key"]
            if saved.get("channels"):
                cfg["channels"] = saved["channels"]
        except Exception:
            pass
    env_key = os.environ.get("GEMINI_API_KEY", "")
    if env_key:
        cfg["gemini_api_key"] = env_key
    return cfg


def save_config(cfg):
    _CONFIG_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
