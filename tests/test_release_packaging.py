"""scripts/package_release.py: the release ZIP never carries secrets, local data, IDE files, caches or macOS metadata."""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("package_release", Path(__file__).resolve().parent.parent / "scripts" / "package_release.py")
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)

SECRET = "gsk_" + "Q7v" * 12          # shaped like a real key, built at run time so this file holds none


def _tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())


PRIVATE = {
    ".env": f"GROQ_API_KEY={SECRET}\nPOSTGRES_PASSWORD=LocalOnlyPw123\n", ".env.local": "X=1",
    "querysure.db": b"SQLite format 3\x00", "data/northwind.sqlite": b"SQLite format 3\x00",
    "tests/fixtures/user/private.csv": "a,b\n1,2\n", "uploads/abc.db": b"x", "reports/r1.html": "<p>",
    ".idea/workspace.xml": "<project/>", ".vscode/settings.json": "{}", "__pycache__/api.cpython-312.pyc": b"\x00",
    "modules/__pycache__/x.pyc": b"\x00", ".pytest_cache/v": "x", ".ruff_cache/x": "x", "node_modules/a/index.js": "x",
    "__MACOSX/._api.py": b"\x00", "._api.py": b"\x00", ".DS_Store": b"\x00", "static/dist/app.1.min.js": "x",
    "certs/server.pem": "-----", "old_release.zip": b"PK",
}
PUBLIC = {
    "api.py": "print('hi')\n", ".env.example": "GROQ_API_KEY=\nPOSTGRES_PASSWORD=\n", "uploads/.gitkeep": "",
    "reports/.gitkeep": "", "tests/test_x.py": "def test_x(): pass\n", "tests/fixtures/build_formats.py": "# builds fixtures\n",
    "docker-compose.yml": "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set it}\n",
    "tests/test_logging.py": "FAKE = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n",       # a deliberately fake key
}


def test_release_contains_only_public_files(tmp_path):
    root = tmp_path / "repo"
    _tree(root, {**PRIVATE, **PUBLIC})
    out = tmp_path / "release.zip"
    names, findings = pkg.build(root, out, pkg.env_secret_values(root / ".env"))
    assert findings == [] and out.exists()
    with zipfile.ZipFile(out) as z:
        shipped = {n[len("querysure/"):] for n in z.namelist()}
        assert all(n.startswith("querysure/") for n in z.namelist())
    assert shipped == set(PUBLIC)
    assert not any("__MACOSX" in n or "/._" in n or n.startswith("._") for n in shipped)


def test_a_leaked_env_value_blocks_the_release(tmp_path):
    root = tmp_path / "repo"
    _tree(root, {**PRIVATE, **PUBLIC, "docker-compose.yml": "POSTGRES_PASSWORD: LocalOnlyPw123\n",
                 "docs/notes.md": f"key: {SECRET}\n"})
    out = tmp_path / "release.zip"
    _, findings = pkg.build(root, out, pkg.env_secret_values(root / ".env"))
    kinds = {(f, why) for f, why in findings}
    assert ("docker-compose.yml", "value of POSTGRES_PASSWORD from .env") in kinds
    assert ("docs/notes.md", "value of GROQ_API_KEY from .env") in kinds
    assert ("docs/notes.md", "groq key") in kinds
    assert not out.exists(), "a failed check must not leave an archive behind"
    assert not any(SECRET in why for _, why in findings), "findings never print the value"


@pytest.mark.parametrize("rel, blocked", [
    (".env", True), (".env.production", True), (".env.example", False), ("a/b/c.sqlite3", True), ("x.db-wal", True),
    (".idea/misc.xml", True), ("static/dist/app.js", True), ("static/js/app.js", False),
    ("tests/fixtures/user/x.json", True), ("tests/fixtures/build_formats.py", False), ("uploads/.gitkeep", False),
    ("migrations/env.py", False), ("evaluation/results/run.json", True),
])
def test_exclusion_rules(rel, blocked):
    assert bool(pkg.excluded(rel)) is blocked


def test_compose_has_no_hardcoded_credentials():
    text = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in text
    assert "JWT_SECRET_KEY: ${JWT_SECRET_KEY:?" in text
    for line in text.splitlines():
        if "DATABASE_URL:" in line:
            assert "${POSTGRES_PASSWORD:?" in line, line
