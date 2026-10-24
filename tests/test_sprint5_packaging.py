"""Sprint 5: the container image must contain what the app imports; frontend assets are content-addressed and cached safely."""
import ast
import os
import re
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()


def _local_import_roots(entrypoints):
    """Transitive set of repo-local top-level modules/packages imported (statically) from the entrypoints."""
    local = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")} | {
        d for d in os.listdir(ROOT)
        # namespace packages (no __init__.py, e.g. observability/) count too: omitting one is an ImportError at startup
        if os.path.isdir(os.path.join(ROOT, d)) and any(f.endswith(".py") for f in os.listdir(os.path.join(ROOT, d)))
    }
    seen, todo = set(), list(entrypoints)
    while todo:
        name = todo.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        path = os.path.join(ROOT, name + ".py") if os.path.isfile(os.path.join(ROOT, name + ".py")) else None
        files = [path] if path else [
            os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(ROOT, name)) for f in fs if f.endswith(".py")]
        for f in files:
            for node in ast.walk(ast.parse(open(f, encoding="utf-8").read())):
                if isinstance(node, ast.Import):
                    todo += [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    todo.append(node.module.split(".")[0])
    return seen


def _copied_in_image(name):
    if os.path.isdir(os.path.join(ROOT, name)):
        return re.search(rf"COPY --from=builder /app/{re.escape(name)}\s", DOCKERFILE) is not None
    return "/app/*.py" in DOCKERFILE or f"/app/{name}.py" in DOCKERFILE


@pytest.mark.parametrize("entrypoint", ["api", "worker", "start", "jobs"])
def test_every_imported_local_module_is_copied_into_the_image(entrypoint):
    missing = sorted(m for m in _local_import_roots([entrypoint]) if not _copied_in_image(m))
    assert not missing, f"Dockerfile does not copy: {missing}"


def test_image_ships_scripts_and_migrations():
    for d in ("scripts", "migrations", "observability", "modules", "static"):
        assert _copied_in_image(d), d
    assert "build_static.py" in DOCKERFILE and "USER 10001:10001" in DOCKERFILE


# ------------------------------------------------------------------------------------------------ assets
@pytest.fixture()
def fresh_assets():
    import api
    dist = os.path.join(ROOT, "static", "dist")
    shutil.rmtree(dist, ignore_errors=True)  # never depend on a build left behind by a developer or an aborted run
    api._ASSET_URLS = None
    yield api
    shutil.rmtree(dist, ignore_errors=True)
    api._ASSET_URLS = None


def test_without_build_sources_get_content_hash_query(client, fresh_assets):
    html = client.get("/").text
    assert re.search(r'/static/js/app\.js\?v=[0-9a-f]{10}"', html)
    assert re.search(r'/static/css/styles\.css\?v=[0-9a-f]{10}"', html)
    r = client.get("/")
    assert r.headers["cache-control"] == "no-cache"  # the shell is always revalidated
    versioned = re.search(r'(/static/js/app\.js\?v=[0-9a-f]{10})', html).group(1)
    assert client.get(versioned).headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/static/js/app.js").headers["cache-control"] == "no-cache"


def test_build_emits_hashed_minified_assets_and_html_uses_them(client, fresh_assets):
    pytest.importorskip("rjsmin")
    import sys
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import build_static
    manifest = build_static.build()
    fresh_assets._ASSET_URLS = None
    html = client.get("/").text
    for url in manifest.values():
        if "app." in url or "styles." in url:
            assert f'"{url}"' in html
            r = client.get(url)
            assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
    js = client.get(manifest["/static/js/app.js"])
    assert len(js.content) < os.path.getsize(os.path.join(ROOT, "static", "js", "app.js"))
    assert "/static/js/app.js" not in html.replace(manifest["/static/js/app.js"], "")  # the unhashed URL is gone
    # the ops shell is rewritten too
    assert manifest["/static/js/ops.js"] in client.get("/ops").text


def test_stale_manifest_falls_back_to_sources(client, fresh_assets):
    dist = os.path.join(ROOT, "static", "dist")
    os.makedirs(dist, exist_ok=True)
    open(os.path.join(dist, "manifest.json"), "w").write('{"/static/js/app.js": "/static/dist/missing.min.js"}')
    fresh_assets._ASSET_URLS = None
    assert "missing.min.js" not in client.get("/").text  # a manifest pointing at absent files is ignored, never served


def test_security_headers_on_shell(client):
    h = client.get("/").headers
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    csp = h["content-security-policy"]
    assert "script-src 'self' https://accounts.google.com" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]
