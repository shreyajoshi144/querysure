#!/usr/bin/env python3
"""
start.py — one-command launcher for QuerySure.

What this does, in order:
  1. Checks Python version and that requirements.txt is installed.
  2. Creates .env from .env.example on first run, and auto-generates a
     persistent JWT_SECRET_KEY if one isn't set (so login tokens survive
     server restarts — without this, every restart invalidates every
     logged-in session, since the app falls back to a random dev secret).
  3. Creates the sample retail.db if it doesn't exist yet.
  4. Starts the API server, which also serves the frontend (static/index.html)
     on the same origin — there's no separate frontend server to run, and no
     CORS setup needed for local use, because api.py mounts /static and
     serves index.html at "/".
  5. Opens your browser to the app once the server is ready.

Usage:
    python start.py
    python start.py --port 8000
    python start.py --no-browser
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
RETAIL_DB_PATH = ROOT / "retail.db"


def _fail(message: str) -> None:
    print(f"\n✗ {message}\n")
    sys.exit(1)


def check_python_version() -> None:
    if sys.version_info < (3, 10):
        _fail(
            f"Python 3.10+ is required (found {sys.version_info.major}.{sys.version_info.minor}). "
            "This project uses syntax (e.g. 'dict | None') that needs a modern interpreter."
        )
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


def check_dependencies(auto_install: bool) -> None:
    required_imports = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("sqlalchemy", "sqlalchemy"),
        ("sqlglot", "sqlglot"),
        ("jwt", "PyJWT"),
        ("bcrypt", "bcrypt"),
        ("pandas", "pandas"),
        ("groq", "groq"),
        ("dotenv", "python-dotenv"),
        ("slowapi", "slowapi"),
    ]
    missing = []
    for module_name, pip_name in required_imports:
        try:
            __import__(module_name)
        except ImportError:
            missing.append(pip_name)

    if not missing:
        print("✓ Dependencies already installed")
        return

    print(f"! Missing dependencies: {', '.join(missing)}")
    if not auto_install:
        _fail("Run: pip install -r requirements.txt")

    print("→ Installing from requirements.txt ...")
    req_file = ROOT / "requirements.txt"
    if not req_file.exists():
        _fail("requirements.txt not found next to start.py")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        _fail(
            "pip install failed. If you're on a system-managed Python install, try:\n"
            f"    {sys.executable} -m pip install -r requirements.txt --break-system-packages\n"
            "or create a virtual environment first:\n"
            "    python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
        )
    print("✓ Dependencies installed")


def ensure_env_file() -> dict:
    """Create .env from .env.example if missing, and make sure JWT_SECRET_KEY
    is set and persisted. Returns the resulting env vars as a dict."""
    if not ENV_PATH.exists():
        if ENV_EXAMPLE_PATH.exists():
            shutil.copy(ENV_EXAMPLE_PATH, ENV_PATH)
            print("✓ Created .env from .env.example")
        else:
            ENV_PATH.write_text("")
            print("✓ Created empty .env")

    lines = ENV_PATH.read_text().splitlines()
    env: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        env[k.strip()] = v.strip()

    changed = False

    if not env.get("JWT_SECRET_KEY"):
        new_secret = secrets.token_urlsafe(64)
        env["JWT_SECRET_KEY"] = new_secret
        _set_env_line(lines, "JWT_SECRET_KEY", new_secret)
        changed = True
        print("✓ Generated a persistent JWT_SECRET_KEY (saved to .env)")

    if changed:
        ENV_PATH.write_text("\n".join(lines) + "\n")

    if not env.get("GROQ_API_KEY") and not env.get("OPENAI_API_KEY") and not env.get("ANTHROPIC_API_KEY"):
        print(
            "! No LLM provider key set (GROQ_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY).\n"
            "  The app will run, but SQL generation will fail until you add one to .env."
        )

    # Push into this process's environment so api.py's load_dotenv + os.environ.get pick them up.
    for k, v in env.items():
        os.environ.setdefault(k, v)

    return env


def _set_env_line(lines: list[str], key: str, value: str) -> None:
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")


def ensure_sample_db() -> None:
    if RETAIL_DB_PATH.exists():
        print("✓ Sample database already exists (retail.db)")
        return
    print("→ Creating sample retail database ...")
    result = subprocess.run([sys.executable, "create_retail.py"], cwd=str(ROOT))
    if result.returncode != 0:
        _fail("Failed to create sample database. Check the traceback above.")
    print("✓ Sample database created")


def open_browser_when_ready(url: str, timeout_seconds: float = 20.0) -> None:
    import urllib.request
    import urllib.error

    def _poll_and_open():
        deadline = time.time() + timeout_seconds
        health_url = url.rstrip("/") + "/api/health"
        while time.time() < deadline:
            try:
                urllib.request.urlopen(health_url, timeout=1)
                webbrowser.open(url)
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.4)

    threading.Thread(target=_poll_and_open, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run QuerySure (backend + frontend, same origin).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab.")
    parser.add_argument("--no-install", action="store_true", help="Don't auto-install missing dependencies.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (development only).")
    args = parser.parse_args()

    print("=" * 60)
    print("QuerySure — setup check")
    print("=" * 60)

    check_python_version()
    check_dependencies(auto_install=not args.no_install)
    ensure_env_file()
    ensure_sample_db()

    print("=" * 60)
    display_host = "localhost" if args.host in ("0.0.0.0", "127.0.0.1") else args.host
    url = f"http://{display_host}:{args.port}"
    print(f"Starting QuerySure at {url}")
    print("The same server answers both the API (/api/*) and the frontend (/),")
    print("so there's nothing separate to run for the UI.")
    print("=" * 60)

    if not args.no_browser:
        open_browser_when_ready(url)

    import uvicorn

    uvicorn.run("api:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()