"""Sprint 4 item 8: Postgres backup/restore + S3 recovery/lifecycle are verified, not just documented."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_lifecycle():
    spec = importlib.util.spec_from_file_location("s3_lifecycle", os.path.join(ROOT, "scripts", "s3_lifecycle.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bucket():
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="qs-test")
        yield c


def test_lifecycle_plan_never_expires_current_user_data():
    lc = _load_lifecycle()
    for rule in lc.plan("datasets")["Rules"]:
        prefix = rule["Filter"]["Prefix"]
        if prefix.startswith("datasets/"):
            assert "Days" not in rule.get("Expiration", {})
    staging = [r for r in lc.plan()["Rules"] if r["Filter"]["Prefix"] == "staging/"][0]
    assert staging["Expiration"]["Days"] == 1


def test_lifecycle_apply_is_idempotent_and_verifiable(bucket):
    lc = _load_lifecycle()
    assert lc.verify(bucket, "qs-test") != []                      # fresh bucket: drift detected
    lc.apply(bucket, "qs-test"); lc.apply(bucket, "qs-test")
    assert lc.verify(bucket, "qs-test") == []
    bucket.put_bucket_versioning(Bucket="qs-test", VersioningConfiguration={"Status": "Suspended"})
    assert "versioning is not Enabled" in lc.verify(bucket, "qs-test")


def test_accidental_delete_and_overwrite_are_recoverable_with_versioning(bucket):
    lc = _load_lifecycle()
    lc.apply(bucket, "qs-test")
    key = "datasets/00000000-0000-0000-0000-000000000001/11111111-1111-1111-1111-111111111111.db"
    bucket.put_object(Bucket="qs-test", Key=key, Body=b"v1")
    bucket.put_object(Bucket="qs-test", Key=key, Body=b"v2-corrupted")          # bad overwrite
    versions = bucket.list_object_versions(Bucket="qs-test", Prefix=key)["Versions"]
    v1 = [v for v in versions if bucket.get_object(Bucket="qs-test", Key=key, VersionId=v["VersionId"])["Body"].read() == b"v1"][0]
    bucket.copy_object(Bucket="qs-test", Key=key, CopySource={"Bucket": "qs-test", "Key": key, "VersionId": v1["VersionId"]})
    assert bucket.get_object(Bucket="qs-test", Key=key)["Body"].read() == b"v1"   # restored by copying the old version forward
    bucket.delete_object(Bucket="qs-test", Key=key)                                # accidental delete -> delete marker
    with pytest.raises(Exception):
        bucket.get_object(Bucket="qs-test", Key=key)
    marker = bucket.list_object_versions(Bucket="qs-test", Prefix=key)["DeleteMarkers"][0]
    bucket.delete_object(Bucket="qs-test", Key=key, VersionId=marker["VersionId"])  # remove marker -> object is back
    assert bucket.get_object(Bucket="qs-test", Key=key)["Body"].read() == b"v1"


@pytest.mark.skipif(not (shutil.which("pg_dump") and shutil.which("pg_restore") and os.environ.get("RESTORE_ADMIN_URL")),
                    reason="needs pg_dump/pg_restore and RESTORE_ADMIN_URL pointing at a Postgres server")
def test_backup_and_verified_restore_round_trip(tmp_path):
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not Postgres")
    out = subprocess.run(["bash", "scripts/pg_backup.sh", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0 and "backup_ok" in out.stdout, out.stderr
    dump = [p for p in tmp_path.iterdir() if p.suffix == ".dump"][0]
    res = subprocess.run(["bash", "scripts/pg_restore_verify.sh", str(dump)], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0 and "restore_verify OK" in res.stdout, res.stdout + res.stderr
    with open(dump, "r+b") as fh:                                   # corrupt it: the verifier must refuse
        fh.seek(2000); fh.write(b"XXXX")
    bad = subprocess.run(["bash", "scripts/pg_restore_verify.sh", str(dump)], cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode != 0
