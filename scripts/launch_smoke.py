#!/usr/bin/env python3
"""Smoke test of a RUNNING QuerySure deployment (docs/LAUNCH_CHECKLIST.md section 4). Standard library only.

    python scripts/launch_smoke.py http://localhost:5099 [--mail-cmd "docker compose -p qs13 exec -T api sh -c 'cat /app/.dev_mail/*.eml'"]

Creates a throw-away account, exercises the real endpoints, deletes the account, prints one PASS/FAIL line per step and exits 1 on
any failure. ``--mail-cmd`` prints the mailbox when EMAIL_PROVIDER=dev, so the verification token can be read; against a real
deployment, verify the e-mail by hand and pass --token-from-stdin instead of --mail-cmd. It never calls a model: steps that need
one (Assistant answers, Agent plans, SQL generation) are reported as SKIPPED, not simulated.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = ""
RESULTS = []


def call(method, path, token=None, body=None, files=None, form=None, raw=False):
    url = BASE + path
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if files or form:
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (form or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (name, content, ctype) in (files or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + content + b"\r\n")
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = r.read()
            return r.status, payload if raw else (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except ValueError:
            return e.code, {"raw": payload[:200].decode("utf-8", "replace")}


def step(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail and not ok else ''}", flush=True)
    return ok


def skipped(name, why):
    print(f"SKIP  {name}  -- {why}", flush=True)


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--mail-cmd", help="shell command that prints the dev mailbox")
    ap.add_argument("--verify-cmd", help="shell command that marks {email} verified (rehearsal only, where no mail server exists)")
    ap.add_argument("--keep-account", action="store_true")
    a = ap.parse_args()
    BASE = a.base_url.rstrip("/")
    email, pw = f"smoke-{uuid.uuid4().hex[:8]}@example.com", "SmokeTest-Passw0rd!"

    s, _ = call("GET", "/api/health")
    step("health", s == 200)
    s, j = call("GET", "/api/ready")
    step("ready (no critical component down)", s == 200, str(j)[:200])
    s, j = call("POST", "/api/auth/register", body={"email": email, "password": pw})
    if not step("register", s == 200, str(j)[:200]):
        return 1
    if not a.mail_cmd and not a.verify_cmd:
        sys.exit("verify the e-mail by hand, then rerun with a mailbox command")
    token = None
    if a.verify_cmd:
        r = subprocess.run(a.verify_cmd.replace("{email}", email), shell=True, capture_output=True, text=True)
        step("e-mail marked verified out of band (no mail server in a rehearsal)", r.returncode == 0, r.stderr[:200])
        s, j = call("POST", "/api/auth/login", body={"email": email, "password": pw})
        token = "skip"
    for _ in range(0 if a.verify_cmd else 20):      # the worker sends the mail
        out = subprocess.run(a.mail_cmd, shell=True, capture_output=True, text=True).stdout
        found = [m for m in re.findall(r"verify_token=([A-Za-z0-9_\-]+)", out) if email in out]
        if found and email in out:
            token = found[-1]
            break
        time.sleep(1.5)
    if not a.verify_cmd:
        step("verification mail delivered", bool(token))
        if not token:
            return 1
        s, j = call("POST", "/api/auth/verify-email?" + urllib.parse.urlencode({"token": token}))
        step("verify e-mail", s == 200, str(j)[:200])
    s, j = call("POST", "/api/auth/login", body={"email": email, "password": pw})
    if not step("login", s == 200, str(j)[:200]):
        return 1
    tok = j["access_token"]
    s, j = call("GET", "/api/workspaces", tok)
    ws = j.get("personal_workspace_id")
    step("personal workspace exists", s == 200 and bool(ws))

    csv = "order_date,region,amount\n" + "\n".join(f"2025-{1 + i % 12:02d}-{1 + i % 27:02d},{'NSEW'[i % 4]},{10 + i % 50}" for i in range(240)) + "\n"
    s, j = call("POST", "/api/session", tok, body={})
    sid = j.get("session_id")
    s, j = call("POST", "/api/connect/upload", tok, form={"session_id": sid, "workspace_id": ws}, files={"file": ("sales.csv", csv.encode(), "text/csv")})
    step("upload CSV", s in (200, 202), str(j)[:200])
    ds = None
    for _ in range(40):
        s, j = call("GET", f"/api/workspaces/{ws}/datasets", tok)
        rows = [d for d in j.get("datasets", []) if d.get("status") == "ready"]
        if rows:
            ds = rows[0]["id"]
            break
        time.sleep(1.5)
    step("dataset becomes ready", bool(ds))
    if not ds:
        return 1
    s, j = call("GET", f"/api/workspaces/{ws}/overview", tok)
    step("workspace overview loads", s == 200 and "datasets" in json.dumps(j), str(j)[:200])
    s, j = call("POST", f"/api/datasets/{ds}/investigations", tok, body={"kind": "trend", "table": "sales", "measure": "amount", "date_column": "order_date"})
    inv = j.get("investigation", {})
    step("investigation runs on real rows", s == 201 and bool(inv.get("report", {}).get("facts") or inv.get("exports")), str(j)[:200])
    html_id = (inv.get("exports") or {}).get("html")
    if html_id:
        s, body = call("GET", f"/api/reports/{html_id}/download", tok, raw=True)
        step("HTML export downloads", s == 200 and b"<html" in body.lower())
    else:
        step("HTML export downloads", False, "no export id")

    doc = b"# Refund policy\n\nRefunds above 500 dollars need approval from the finance manager within 14 days.\n"
    s, j = call("POST", f"/api/workspaces/{ws}/knowledge", tok, files={"file": ("policy.md", doc, "text/markdown")})
    doc_id = (j.get("document") or {}).get("id")
    step("knowledge document indexed", s == 201 and bool(doc_id), str(j)[:200])
    s, j = call("GET", f"/api/workspaces/{ws}/knowledge/search?" + urllib.parse.urlencode({"q": "who approves large refunds"}), tok)
    step("passage search finds it", s == 200 and any("finance manager" in r["text"] for r in j.get("results", [])), str(j)[:200])
    s, j = call("DELETE", f"/api/knowledge/{doc_id}", tok)
    step("document deleted", s == 200, str(j)[:200])
    skipped("Assistant answer / Agent plan / SQL generation", "need a funded provider key; run evaluation live separately")

    s, j = call("GET", "/api/auth/me", tok)
    if a.keep_account:
        return 0 if all(RESULTS) else 1
    s, j = call("POST", "/api/account/delete", tok, body={"confirm_email": email, "password": pw})
    step("throw-away account deleted", s == 200, str(j)[:200])
    s, _ = call("GET", "/api/auth/me", tok)
    step("deleted account's token no longer works", s in (401, 403, 404))
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} steps passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
