"""Build content-hashed, minified frontend assets (run at image/deploy build time).

    python scripts/build_static.py

Reads the canonical sources (static/js/app.js, static/js/scene.js, static/js/ops.js, static/css/styles.css, static/css/ops.css), writes
``static/dist/<name>.<hash>.min.<ext>`` and ``static/dist/manifest.json``. When the manifest exists the server serves the
HTML with those URLs and marks them immutable (a new build changes the URL, so browsers can never run stale code). Without a
build the server falls back to the unminified sources with a ``?v=<content hash>`` query, so development needs no build step.
Needs ``rjsmin`` and ``rcssmin`` (requirements-dev.txt). Only whitespace and comments are removed; no identifier is renamed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
DIST = os.path.join(STATIC, "dist")
ASSETS = ("js/app.js", "js/scene.js", "js/ops.js", "css/styles.css", "css/ops.css")


def build() -> dict:
    import rcssmin
    import rjsmin

    os.makedirs(DIST, exist_ok=True)
    for name in os.listdir(DIST):  # drop stale builds so the directory never accumulates old bundles
        os.remove(os.path.join(DIST, name))
    manifest = {}
    for rel in ASSETS:
        with open(os.path.join(STATIC, rel), "r", encoding="utf-8") as fh:
            source = fh.read()
        minified = rjsmin.jsmin(source) if rel.endswith(".js") else rcssmin.cssmin(source)
        digest = hashlib.sha256(minified.encode("utf-8")).hexdigest()[:10]
        stem, ext = os.path.splitext(os.path.basename(rel))
        out = f"{stem}.{digest}.min{ext}"
        with open(os.path.join(DIST, out), "w", encoding="utf-8") as fh:
            fh.write(minified)
        manifest["/static/" + rel] = "/static/dist/" + out
    with open(os.path.join(DIST, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    return manifest


if __name__ == "__main__":
    try:
        for src, dst in build().items():
            print(f"{src} -> {dst}")
    except ImportError:
        print("rjsmin / rcssmin are required: pip install -r requirements-dev.txt", file=sys.stderr)
        sys.exit(1)
