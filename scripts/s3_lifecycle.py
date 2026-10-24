#!/usr/bin/env python3
"""S3 durability + lifecycle for QuerySure (Sprint 4).  Usage:
    python scripts/s3_lifecycle.py plan            # print the configuration (no AWS calls)
    python scripts/s3_lifecycle.py apply           # enable versioning + install lifecycle rules (idempotent)
    python scripts/s3_lifecycle.py verify          # read back and FAIL (exit 1) if the bucket drifted from the plan
Env: S3_BUCKET (required for apply/verify), S3_PREFIX (default "datasets"), S3_REGION, S3_ENDPOINT_URL.

Rules (all values overridable by env):
  staging/            upload staging objects: expire after STAGING_EXPIRE_DAYS (1). Normal flow deletes them right after ingestion;
                      this is the backstop for crashed/reaped jobs.
  (whole bucket)      abort incomplete multipart uploads after 7 days.
  <prefix>/ + <prefix>/reports/   current objects are NEVER auto-expired (user data; the app deletes them when the user does).
                      With versioning on, an overwritten/deleted object stays recoverable: noncurrent versions expire after
                      NONCURRENT_EXPIRE_DAYS (30) and expired delete markers are cleaned up.
"""
from __future__ import annotations

import json
import os
import sys

STAGING_EXPIRE_DAYS = int(os.environ.get("STAGING_EXPIRE_DAYS", "1"))
NONCURRENT_EXPIRE_DAYS = int(os.environ.get("NONCURRENT_EXPIRE_DAYS", "30"))
MULTIPART_ABORT_DAYS = int(os.environ.get("MULTIPART_ABORT_DAYS", "7"))


def plan(prefix: str = "datasets") -> dict:
    return {"Rules": [
        {"ID": "qs-expire-upload-staging", "Status": "Enabled", "Filter": {"Prefix": "staging/"},
         "Expiration": {"Days": STAGING_EXPIRE_DAYS}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
        {"ID": "qs-abort-incomplete-multipart", "Status": "Enabled", "Filter": {"Prefix": ""},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": MULTIPART_ABORT_DAYS}},
        {"ID": "qs-expire-noncurrent-datasets", "Status": "Enabled", "Filter": {"Prefix": f"{prefix}/"},
         "NoncurrentVersionExpiration": {"NoncurrentDays": NONCURRENT_EXPIRE_DAYS},
         "Expiration": {"ExpiredObjectDeleteMarker": True}},
    ]}


def _client():
    import boto3
    kw = {}
    if os.environ.get("S3_REGION"):
        kw["region_name"] = os.environ["S3_REGION"]
    if os.environ.get("S3_ENDPOINT_URL"):
        kw["endpoint_url"] = os.environ["S3_ENDPOINT_URL"]
    return boto3.client("s3", **kw)


def apply(client, bucket: str, prefix: str = "datasets") -> None:
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=plan(prefix))


def verify(client, bucket: str, prefix: str = "datasets") -> list:
    """Return a list of problems (empty = bucket matches the plan)."""
    problems = []
    if client.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        problems.append("versioning is not Enabled")
    try:
        rules = {r["ID"]: r for r in client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]}
    except Exception as exc:  # noqa: BLE001
        return problems + [f"no lifecycle configuration ({type(exc).__name__})"]
    for want in plan(prefix)["Rules"]:
        got = rules.get(want["ID"])
        if got is None:
            problems.append(f"missing rule {want['ID']}")
        elif got.get("Status") != "Enabled":
            problems.append(f"rule {want['ID']} disabled")
    staging = rules.get("qs-expire-upload-staging", {})
    if staging.get("Expiration", {}).get("Days") != STAGING_EXPIRE_DAYS:
        problems.append("staging expiry differs from plan")
    for r in rules.values():       # a rule that could expire live user data would be a data-loss bug
        if r.get("Filter", {}).get("Prefix", "").startswith((f"{prefix}/", f"{prefix}/reports/")) and \
                r.get("Expiration", {}).get("Days") is not None:
            problems.append(f"rule {r['ID']} would expire current user data")
    return problems


def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "plan"
    prefix = os.environ.get("S3_PREFIX", "datasets").strip("/") or "datasets"
    if cmd == "plan":
        print(json.dumps(plan(prefix), indent=2)); return 0
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        print("S3_BUCKET is required", file=sys.stderr); return 2
    c = _client()
    if cmd == "apply":
        apply(c, bucket, prefix); print("applied"); return 0
    if cmd == "verify":
        problems = verify(c, bucket, prefix)
        print("OK" if not problems else "PROBLEMS: " + "; ".join(problems))
        return 1 if problems else 0
    print(__doc__); return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
