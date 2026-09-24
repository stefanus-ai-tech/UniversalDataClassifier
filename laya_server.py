"""Run the local Laya service using this project's .env file."""

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

origin = urlparse(os.getenv("LAYA_URL", "http://127.0.0.1:8001"))
if origin.hostname not in {"127.0.0.1", "localhost"}:
    raise SystemExit("Local launcher requires LAYA_URL to point to localhost")
os.environ.setdefault("LAYA_HOST", "127.0.0.1")
os.environ.setdefault("LAYA_PORT", str(origin.port or 8001))
os.environ.setdefault("LAYA_DEVICE", "cpu")
os.environ.setdefault("LAYA_PRELOAD", "1")
os.environ.setdefault("LAYA_MODELS", "multilingual")
os.environ.setdefault("LAYA_THREADS", "4")

from laya.serve import main  # noqa: E402

if __name__ == "__main__":
    main()
