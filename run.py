"""Start LeadFlow:  python run.py   (then open http://localhost:5000)"""
import os
from pathlib import Path


def _load_env_file():
    """Read KEY=VALUE lines from a local .env file (real environment variables win)."""
    f = Path(__file__).resolve().parent / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    try:
        from waitress import serve  # production-grade server (pip install waitress)
        print(f"LeadFlow running on http://localhost:{port}  (waitress)")
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print(f"LeadFlow running on http://localhost:{port}  (Flask server; pip install waitress for production)")
        app.run(host=host, port=port, threaded=True)
