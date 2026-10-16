"""
Unit tests for lambda/snapshot_cleanup/main.py
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from tests.lambda_loader import load_lambda_module

snapshot_cleanup = load_lambda_module("snapshot_cleanup", "snapshot_cleanup_main")

ACCOUNT = "123456789012"


def _snapshot(snapshot_id, age_days, tags=None):
    return {
        "SnapshotId": snapshot_id,
        "StartTime": datetime.now(timezone.utc) - timedelta(days=age_days),
        "Tags": tags or [],
    }


def _ec2_with_snapshots(snapshots):
    ec2 = MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [{"Snapshots": snapshots}]
    return ec2


class TestCleanupSnapshots:

    def test_dry_run_reports_but_never_deletes(self):
        ec2 = _ec2_with_snapshots([_snapshot("snap-old", age_days=45)])
        deleted = snapshot_cleanup.cleanup_snapshots(ec2, ACCOUNT, 30, dry_run=True)
        assert deleted == ["snap-old"]
        ec2.delete_snapshot.assert_not_called()

    def test_deletes_old_snapshot_when_not_dry_run(self):
        ec2 = _ec2_with_snapshots([_snapshot("snap-old", age_days=45)])
        deleted = snapshot_cleanup.cleanup_snapshots(ec2, ACCOUNT, 30, dry_run=False)
        assert deleted == ["snap-old"]
        ec2.delete_snapshot.assert_called_once_with(SnapshotId="snap-old")

    def test_recent_snapshot_kept(self):
        ec2 = _ec2_with_snapshots([_snapshot("snap-new", age_days=5)])
        deleted = snapshot_cleanup.cleanup_snapshots(ec2, ACCOUNT, 30, dry_run=False)
        assert deleted == []
        ec2.delete_snapshot.assert_not_called()

    def test_retain_tag_always_respected(self):
        ec2 = _ec2_with_snapshots([
            _snapshot("snap-keep", age_days=400, tags=[{"Key": "Retain", "Value": "true"}])
        ])
        deleted = snapshot_cleanup.cleanup_snapshots(ec2, ACCOUNT, 30, dry_run=False)
        assert deleted == []
        ec2.delete_snapshot.assert_not_called()

    def test_scopes_scan_to_account_snapshots(self):
        ec2 = _ec2_with_snapshots([])
        snapshot_cleanup.cleanup_snapshots(ec2, ACCOUNT, 30, dry_run=True)
        assert ec2.get_paginator.return_value.paginate.call_args.kwargs["OwnerIds"] == [ACCOUNT]

    def test_in_use_snapshot_error_does_not_stop_the_run(self):
        ec2 = _ec2_with_snapshots([
            _snapshot("snap-ami", age_days=45),
            _snapshot("snap-free", age_days=45),
        ])
        ec2.delete_snapshot.side_effect = [
            ClientError({"Error": {"Code": "InvalidSnapshot.InUse"}}, "DeleteSnapshot"),
            {},
        ]
        deleted = snapshot_cleanup.cleanup_snapshots(ec2, ACCOUNT, 30, dry_run=False)
        assert deleted == ["snap-free"]


class TestLambdaHandler:
    """The handler owns the DRY_RUN switch. The helper tests pass dry_run
    explicitly, so the fail-safe default lived entirely unexercised."""

    def _run(self, snapshots, monkeypatch):
        ec2 = _ec2_with_snapshots(snapshots)
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT}

        def client(service, **kwargs):
            return {"ec2": ec2, "sts": sts, "sns": MagicMock()}[service]

        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
        with patch.object(snapshot_cleanup.boto3, "client", side_effect=client):
            return snapshot_cleanup.lambda_handler({}, None), ec2

    def test_dry_run_is_the_default_with_no_env_set(self, monkeypatch):
        """With DRY_RUN unset the function must report, never delete — the
        shipped-safety guarantee for a brand-new deployment."""
        monkeypatch.delenv("DRY_RUN", raising=False)
        monkeypatch.delenv("RETENTION_DAYS", raising=False)
        result, ec2 = self._run([_snapshot("snap-old", age_days=45)], monkeypatch)

        summary = json.loads(result["body"])
        assert summary["DryRun"] is True
        assert summary["RetentionDays"] == 30
        assert summary["DeletedSnapshots"] == ["snap-old"]
        ec2.delete_snapshot.assert_not_called()

    def test_dry_run_false_enables_deletion(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        result, ec2 = self._run([_snapshot("snap-old", age_days=45)], monkeypatch)

        assert json.loads(result["body"])["DryRun"] is False
        ec2.delete_snapshot.assert_called_once_with(SnapshotId="snap-old")

    def test_dry_run_flag_is_case_insensitive(self, monkeypatch):
        """Terraform writes "true"/"false"; an operator may type "FALSE"."""
        monkeypatch.setenv("DRY_RUN", "FALSE")
        result, ec2 = self._run([_snapshot("snap-old", age_days=45)], monkeypatch)

        assert json.loads(result["body"])["DryRun"] is False
        ec2.delete_snapshot.assert_called_once()

    def test_any_other_value_stays_in_dry_run(self, monkeypatch):
        """Only an explicit "false" arms deletion — anything else fails safe."""
        monkeypatch.setenv("DRY_RUN", "no")
        result, ec2 = self._run([_snapshot("snap-old", age_days=45)], monkeypatch)

        assert json.loads(result["body"])["DryRun"] is True
        ec2.delete_snapshot.assert_not_called()

    def test_cleared_dry_run_value_stays_in_dry_run(self, monkeypatch):
        """The realistic accident: an operator blanks the field in the Lambda
        console. An empty value must not silently become destructive."""
        monkeypatch.setenv("DRY_RUN", "")
        result, ec2 = self._run([_snapshot("snap-old", age_days=45)], monkeypatch)

        assert json.loads(result["body"])["DryRun"] is True
        ec2.delete_snapshot.assert_not_called()

    def test_unrecognised_dry_run_values_stay_in_dry_run(self, monkeypatch):
        """Malformed, misspelt, padded and alternate-spelling values are all
        treated as destructive-mode requests only when they literally read
        "false" — anything else keeps snapshots on the server."""
        for value in ("0", "flase", "False ", "FALSE!", "yes", "1", "disabled", "nope"):
            monkeypatch.setenv("DRY_RUN", value)
            result, ec2 = self._run([_snapshot("snap-old", age_days=45)], monkeypatch)

            summary = json.loads(result["body"])
            assert summary["DryRun"] is True, f"DRY_RUN={value!r} left dry-run mode"
            ec2.delete_snapshot.assert_not_called()

    def test_sns_subject_reports_dry_run_for_unexpected_values(self, monkeypatch):
        """The published summary must tell the operator which mode ran, so a
        mis-set DRY_RUN shows up in the alert rather than in deleted data."""
        monkeypatch.setenv("DRY_RUN", "0")
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        sns = MagicMock()
        ec2 = _ec2_with_snapshots([_snapshot("snap-old", age_days=45)])
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT}

        def client(service, **kwargs):
            return {"ec2": ec2, "sts": sts, "sns": sns}[service]

        with patch.object(snapshot_cleanup.boto3, "client", side_effect=client):
            snapshot_cleanup.lambda_handler({}, None)

        assert "(dry run)" in sns.publish.call_args.kwargs["Subject"]
        ec2.delete_snapshot.assert_not_called()

    def test_retention_days_read_from_environment(self, monkeypatch):
        monkeypatch.setenv("RETENTION_DAYS", "7")
        result, ec2 = self._run([_snapshot("snap-10d", age_days=10)], monkeypatch)

        summary = json.loads(result["body"])
        assert summary["RetentionDays"] == 7
        assert summary["DeletedSnapshots"] == ["snap-10d"]
        ec2.delete_snapshot.assert_not_called()

    def test_scans_only_this_account(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        _, ec2 = self._run([], monkeypatch)
        assert ec2.get_paginator.return_value.paginate.call_args.kwargs["OwnerIds"] == [ACCOUNT]


class TestPublishSummary:

    def test_skips_without_topic(self, monkeypatch):
        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
        with patch.object(snapshot_cleanup.boto3, "client") as client:
            snapshot_cleanup.publish_summary({"Count": 1, "DryRun": True})
        client.assert_not_called()

    def test_skips_when_nothing_found(self, monkeypatch):
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        with patch.object(snapshot_cleanup.boto3, "client") as client:
            snapshot_cleanup.publish_summary({"Count": 0, "DryRun": True})
        client.assert_not_called()

    def test_subject_labels_dry_run_mode(self, monkeypatch):
        """An operator must be able to tell 'would delete' from 'did delete'
        from the email subject alone."""
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        sns = MagicMock()
        with patch.object(snapshot_cleanup.boto3, "client", return_value=sns):
            snapshot_cleanup.publish_summary({"Count": 2, "DryRun": True})
        assert "2 snapshot(s) (dry run)" in sns.publish.call_args.kwargs["Subject"]

    def test_subject_labels_real_deletions(self, monkeypatch):
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        sns = MagicMock()
        with patch.object(snapshot_cleanup.boto3, "client", return_value=sns):
            snapshot_cleanup.publish_summary({"Count": 2, "DryRun": False})
        assert "2 snapshot(s) (deleted)" in sns.publish.call_args.kwargs["Subject"]

    def test_sns_failure_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        sns = MagicMock()
        sns.publish.side_effect = RuntimeError("sns down")
        with patch.object(snapshot_cleanup.boto3, "client", return_value=sns):
            snapshot_cleanup.publish_summary({"Count": 1, "DryRun": False})
