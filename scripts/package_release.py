"""Build the release ZIP of this repository, and refuse to finish if anything private would ship.

    python scripts/package_release.py --out ../QuerySure_Final_Release.zip
    python scripts/package_release.py --scan-git        # scan the files git tracks (or would add) instead

Finder's "Compress" and plain `zip -r` ignore .gitignore and add macOS metadata, so a hand-made archive of the working
folder carries .env, local databases, private evaluation data, IDE settings and caches. This script decides what ships
itself (it does not rely on .gitignore), writes the archive with Python's zipfile (no __MACOSX, no ._ files), then
re-reads the archive and fails when an excluded path or a secret is found. Findings name the file and the kind of
match, never the value.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
TOP = "querysure"

# Directories never shipped, wherever they appear.
EXCLUDED_DIRS = {".git", ".idea", ".vscode", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules",
                 "venv", ".venv", "env", "__MACOSX", ".deepeval", ".dev_mail", ".cache", "htmlcov", "dist", "build"}
# Repository paths never shipped (runtime data and the owner's private evaluation files). Placeholders stay.
EXCLUDED_PREFIXES = ("static/dist/", "tests/fixtures/user/", "uploads/", "reports/", "output/", "evaluation/results/",
                     "loadtests/results/tmp/")
KEEP_PLACEHOLDERS = {"uploads/.gitkeep", "reports/.gitkeep", "output/.gitkeep", "evaluation/results/.gitkeep"}
# File names never shipped.
EXCLUDED_FILES = [".env", ".env.*", "*.db", "*.sqlite", "*.sqlite3", "*.db-journal", "*.db-wal", "*.db-shm", "dump.rdb",
                  ".DS_Store", "._*", "Thumbs.db", "*.pyc", "*.pyo", "*.log", "*.pem", "*.key", "*.p12", "*.pfx",
                  ".coverage", "*.zip", "*.swp"]
ALLOWED_FILES = {".env.example"}

SECRET_PATTERNS = {
    "groq key": r"gsk_[A-Za-z0-9]{20,}",
    "openai key": r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    "anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "aws access key": r"AKIA[0-9A-Z]{16}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "slack token": r"xox[abpr]-[A-Za-z0-9-]{10,}",
    "google oauth secret": r"GOCSPX-[A-Za-z0-9_-]{20,}",
    "github token": r"gh[pousr]_[A-Za-z0-9]{30,}",
}
# Deliberately fake values used by the log-redaction and leak tests.
_FAKE = re.compile(r"abcdefghij|ABCDEFGHIJ|CANARY|SECRET-should-never-leak|LEAKY|EXAMPLE|example|0123456789abcdef", re.I)
_SECRET_VARS = re.compile(r"KEY|SECRET|PASSWORD|TOKEN|CREDENTIAL|DSN", re.I)


def excluded(rel: str) -> Optional[str]:
    """Why `rel` (a POSIX path relative to the repository root) must not ship, or None."""
    if rel in KEEP_PLACEHOLDERS:
        return None
    parts = rel.split("/")
    for d in parts[:-1]:
        if d in EXCLUDED_DIRS:
            return f"directory {d}/"
    for prefix in EXCLUDED_PREFIXES:
        if rel.startswith(prefix):
            return f"path {prefix}"
    name = parts[-1]
    if name in ALLOWED_FILES:
        return None
    for pat in EXCLUDED_FILES:
        if fnmatch.fnmatch(name, pat):
            return f"file pattern {pat}"
    return None


def candidate_files(root: Path) -> List[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for f in sorted(filenames):
            out.append(f if rel_dir == "." else f"{rel_dir}/{f}")
    return out


def env_secret_values(env_path: Path) -> Dict[str, str]:
    """Secret-looking values from a local .env (names only are ever reported)."""
    vals: Dict[str, str] = {}
    if not env_path.is_file():
        return vals
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        name, value = m.group(1), m.group(2).strip().strip("'\"")
        if _SECRET_VARS.search(name) and len(value) >= 8:
            vals[name] = value
        if name.endswith("_URL") and re.search(r"://[^:/@]+:([^@]{6,})@", value):
            vals[name + " password"] = re.search(r"://[^:/@]+:([^@]{6,})@", value).group(1)
    return vals


def scan_text(rel: str, text: str, secrets: Dict[str, str]) -> List[Tuple[str, str]]:
    findings = []
    for name, value in secrets.items():
        if value in text:
            findings.append((rel, f"value of {name} from .env"))
    for kind, pat in SECRET_PATTERNS.items():
        for m in re.finditer(pat, text):
            if not _FAKE.search(m.group(0)):
                findings.append((rel, kind))
    return findings


def scan_files(items: Iterable[Tuple[str, bytes]], secrets: Dict[str, str]) -> List[Tuple[str, str]]:
    findings: List[Tuple[str, str]] = []
    for rel, data in items:
        why = excluded(rel)
        if why:
            findings.append((rel, f"excluded path ({why})"))
            continue
        if b"\x00" in data[:4096]:
            continue                                         # binary (fonts, images): no text secrets to match
        findings += scan_text(rel, data.decode("utf-8", errors="ignore"), secrets)
    return findings


def build(root: Path, out: Path, secrets: Dict[str, str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    files = [rel for rel in candidate_files(root) if not excluded(rel)]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(root / rel, f"{TOP}/{rel}")
    # verify what was actually written, not what was intended
    with zipfile.ZipFile(tmp) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        bad_top = [n for n in names if not n.startswith(TOP + "/")]
        findings = [(n, "outside the top folder") for n in bad_top]
        findings += scan_files(((n[len(TOP) + 1:], z.read(n)) for n in names if n.startswith(TOP + "/")), secrets)
    if findings:
        tmp.unlink()
    else:
        tmp.replace(out)
    return names, findings


def git_files(root: Path) -> List[str]:
    res = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root,
                         capture_output=True, text=True, check=True)
    return [line for line in res.stdout.splitlines() if line]


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    p.add_argument("--out", default=str(ROOT.parent / "QuerySure_Final_Release.zip"), help="ZIP to write")
    p.add_argument("--env", default=None, help=".env whose secret values must not appear (default: <root>/.env)")
    p.add_argument("--scan-git", action="store_true", help="scan the files git tracks or would add, build nothing")
    a = p.parse_args(argv)
    root = Path(a.root).resolve()
    secrets = env_secret_values(Path(a.env) if a.env else root / ".env")
    if a.scan_git:
        files = git_files(root)
        findings = scan_files(((rel, (root / rel).read_bytes()) for rel in files if (root / rel).is_file()), secrets)
        print(f"git files scanned: {len(files)}; .env values checked: {len(secrets)}")
    else:
        names, findings = build(root, Path(a.out).resolve(), secrets)
        print(f"files in archive: {len(names)}; .env values checked: {len(secrets)}")
    for rel, why in findings:
        print(f"  BLOCKED {rel}: {why}")
    if findings:
        print("FAILED: nothing was written." if not a.scan_git else "FAILED")
        return 1
    print("OK: no excluded paths and no secrets found." + ("" if a.scan_git else f" Wrote {Path(a.out).resolve()}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
