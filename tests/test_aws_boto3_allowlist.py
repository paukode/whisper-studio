"""aws_boto3 must use a read-only allowlist (not a write denylist), so
read-shaped-but-mutating methods like assume_role are blocked, while genuine
reads pass."""

from server.executors import code as codemod


def test_blocks_read_shaped_but_mutating_method():
    # assume_role is not a write-prefix, so the old denylist let it through.
    out = codemod.exec_aws_boto3({"service": "sts", "method": "assume_role"}, "", {})
    assert out.startswith("Blocked")


def test_blocks_obvious_write():
    out = codemod.exec_aws_boto3({"service": "s3", "method": "create_bucket"}, "", {})
    assert out.startswith("Blocked")


def test_allows_read_method(monkeypatch):
    class FakeClient:
        def list_buckets(self, **kwargs):
            return {"Buckets": [], "ResponseMetadata": {"x": 1}}

    monkeypatch.setattr(codemod.boto3, "client", lambda *a, **k: FakeClient())
    out = codemod.exec_aws_boto3({"service": "s3", "method": "list_buckets", "params": {}}, "", {})
    assert "Buckets" in out
    assert "ResponseMetadata" not in out  # stripped by the executor
