"""
Unit tests for unused_resources.py
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from unused_resources import PRICING, UnusedResourceFinder, build_summary


def _finder():
    f = UnusedResourceFinder.__new__(UnusedResourceFinder)
    f.ec2 = MagicMock()
    f.cloudwatch = MagicMock()
    f.rds = MagicMock()
    f.region = "us-east-1"
    return f


def _datapoints(values, statistic="Average"):
    return {"Datapoints": [{statistic: v} for v in values]}


class TestIdleEC2:

    def _with_instance(self, finder, cpu_datapoints):
        finder.ec2.get_paginator.return_value.paginate.return_value = [
            {"Reservations": [{"Instances": [
                {"InstanceId": "i-1", "InstanceType": "t3.medium"}
            ]}]}
        ]
        finder.cloudwatch.get_metric_statistics.return_value = _datapoints(cpu_datapoints)

    def test_flags_idle_instance(self):
        finder = _finder()
        self._with_instance(finder, [1.0, 2.5, 0.8])
        findings = finder.find_idle_ec2_instances(cpu_threshold=5.0, days=14)
        assert len(findings) == 1
        assert findings[0]["InstanceId"] == "i-1"
        assert findings[0]["EstimatedMonthlySavingsUSD"] is None

    def test_busy_instance_not_flagged(self):
        finder = _finder()
        self._with_instance(finder, [1.0, 60.0])
        assert finder.find_idle_ec2_instances(cpu_threshold=5.0, days=14) == []

    def test_no_metrics_not_flagged(self):
        finder = _finder()
        self._with_instance(finder, [])
        assert finder.find_idle_ec2_instances() == []


class TestUnattachedVolumes:

    def test_savings_use_volume_type_price(self):
        finder = _finder()
        finder.ec2.get_paginator.return_value.paginate.return_value = [
            {"Volumes": [{"VolumeId": "vol-1", "VolumeType": "gp2", "Size": 100}]}
        ]
        findings = finder.find_unattached_ebs_volumes()
        assert len(findings) == 1
        expected = round(100 * PRICING["ebs_gb_month"]["gp2"], 2)
        assert findings[0]["EstimatedMonthlySavingsUSD"] == expected

    def test_unknown_volume_type_falls_back_to_gp3_price(self):
        finder = _finder()
        finder.ec2.get_paginator.return_value.paginate.return_value = [
            {"Volumes": [{"VolumeId": "vol-1", "VolumeType": "future-type", "Size": 10}]}
        ]
        findings = finder.find_unattached_ebs_volumes()
        assert findings[0]["EstimatedMonthlySavingsUSD"] == round(
            10 * PRICING["ebs_gb_month"]["gp3"], 2
        )


class TestUnusedElasticIPs:

    def test_only_unassociated_addresses_flagged(self):
        finder = _finder()
        finder.ec2.describe_addresses.return_value = {
            "Addresses": [
                {"PublicIp": "1.1.1.1", "AllocationId": "eip-1", "InstanceId": "i-1"},
                {"PublicIp": "2.2.2.2", "AllocationId": "eip-2"},
            ]
        }
        findings = finder.find_unused_elastic_ips()
        assert len(findings) == 1
        assert findings[0]["PublicIp"] == "2.2.2.2"
        assert findings[0]["EstimatedMonthlySavingsUSD"] == PRICING["elastic_ip_month"]


class TestOldSnapshots:

    def _with_snapshots(self, finder, snapshots):
        finder.ec2.get_paginator.return_value.paginate.return_value = [
            {"Snapshots": snapshots}
        ]

    def test_old_snapshot_flagged_with_savings(self):
        finder = _finder()
        self._with_snapshots(finder, [{
            "SnapshotId": "snap-1",
            "VolumeSize": 40,
            "StartTime": datetime.now(timezone.utc) - timedelta(days=90),
        }])
        with patch("unused_resources.boto3.client") as client:
            client.return_value.get_caller_identity.return_value = {"Account": "123"}
            findings = finder.find_old_ebs_snapshots(retention_days=30)
        assert len(findings) == 1
        assert findings[0]["EstimatedMonthlySavingsUSD"] == round(
            40 * PRICING["snapshot_gb_month"], 2
        )

    def test_retain_tag_skipped(self):
        finder = _finder()
        self._with_snapshots(finder, [{
            "SnapshotId": "snap-1",
            "VolumeSize": 40,
            "StartTime": datetime.now(timezone.utc) - timedelta(days=90),
            "Tags": [{"Key": "Retain", "Value": "compliance"}],
        }])
        with patch("unused_resources.boto3.client") as client:
            client.return_value.get_caller_identity.return_value = {"Account": "123"}
            findings = finder.find_old_ebs_snapshots(retention_days=30)
        assert findings == []

    def test_snapshot_within_retention_not_flagged(self):
        """The cutoff branch protects a perfectly healthy recent snapshot from
        being reported as waste."""
        finder = _finder()
        self._with_snapshots(finder, [{
            "SnapshotId": "snap-new",
            "VolumeSize": 40,
            "StartTime": datetime.now(timezone.utc) - timedelta(days=5),
        }])
        with patch("unused_resources.boto3.client") as client:
            client.return_value.get_caller_identity.return_value = {"Account": "123"}
            findings = finder.find_old_ebs_snapshots(retention_days=30)
        assert findings == []

    def test_missing_volume_size_costs_zero(self):
        """Snapshots with no VolumeSize report 0 rather than raising."""
        finder = _finder()
        self._with_snapshots(finder, [{
            "SnapshotId": "snap-1",
            "StartTime": datetime.now(timezone.utc) - timedelta(days=90),
        }])
        with patch("unused_resources.boto3.client") as client:
            client.return_value.get_caller_identity.return_value = {"Account": "123"}
            findings = finder.find_old_ebs_snapshots(retention_days=30)
        assert findings[0]["EstimatedMonthlySavingsUSD"] == 0.0


class TestIdleRDS:

    def _with_db(self, finder, connection_datapoints, status="available"):
        finder.rds.get_paginator.return_value.paginate.return_value = [
            {"DBInstances": [{
                "DBInstanceIdentifier": "db-1",
                "DBInstanceClass": "db.t3.medium",
                "Engine": "postgres",
                "DBInstanceStatus": status,
            }]}
        ]
        finder.cloudwatch.get_metric_statistics.return_value = _datapoints(
            connection_datapoints, statistic="Maximum"
        )

    def test_zero_connections_flagged(self):
        finder = _finder()
        self._with_db(finder, [0.0, 0.0, 0.0])
        findings = finder.find_idle_rds_instances(days=14)
        assert len(findings) == 1
        assert findings[0]["DBInstanceIdentifier"] == "db-1"

    def test_active_database_not_flagged(self):
        finder = _finder()
        self._with_db(finder, [0.0, 12.0])
        assert finder.find_idle_rds_instances(days=14) == []

    def test_stopped_database_not_scanned(self):
        finder = _finder()
        self._with_db(finder, [0.0], status="stopped")
        assert finder.find_idle_rds_instances(days=14) == []


class TestBuildSummary:

    def test_totals_and_unquantified_count(self):
        summary = build_summary({
            "UnattachedEBSVolumes": [
                {"EstimatedMonthlySavingsUSD": 8.0},
                {"EstimatedMonthlySavingsUSD": 2.5},
            ],
            "IdleEC2Instances": [{"EstimatedMonthlySavingsUSD": None}],
        })
        assert summary["TotalFindings"] == 3
        assert summary["EstimatedMonthlySavingsUSD"] == 10.5
        assert summary["UnquantifiedFindings"] == 1
        assert summary["FindingsByType"]["UnattachedEBSVolumes"] == 2


class TestConstructor:

    def test_clients_are_scoped_to_the_given_region(self):
        """The other tests bypass __new__, so this is the only place the real
        client wiring — and the region that must not leak across scans — runs."""
        with patch("unused_resources.boto3.client") as client:
            finder = UnusedResourceFinder(region="ap-south-1")

        services = {call.args[0] for call in client.call_args_list}
        assert services == {"ec2", "cloudwatch", "rds"}
        assert {call.kwargs["region_name"] for call in client.call_args_list} == {"ap-south-1"}
        assert finder.region == "ap-south-1"


class TestScanAll:

    _CATEGORIES = {
        "IdleEC2Instances", "UnattachedEBSVolumes", "UnusedElasticIPs",
        "OldEBSSnapshots", "IdleRDSInstances",
    }

    def test_returns_every_finding_category(self):
        finder = _finder()
        finder.ec2.get_paginator.return_value.paginate.return_value = []
        finder.ec2.describe_addresses.return_value = {"Addresses": []}
        finder.rds.get_paginator.return_value.paginate.return_value = []
        with patch("unused_resources.boto3.client") as client:
            client.return_value.get_caller_identity.return_value = {"Account": "123"}
            results = finder.scan_all(cpu_threshold=5.0, ec2_days=14, snapshot_days=30)

        assert set(results) == self._CATEGORIES
        assert all(findings == [] for findings in results.values())

    def test_thresholds_forwarded_to_each_detector(self):
        """scan_all's positional args map cpu/threshold/retention onto the right
        detectors — a swap here would silently mis-scan every region."""
        finder = _finder()
        with patch.object(finder, "find_idle_ec2_instances", return_value=[]) as idle_ec2, \
                patch.object(finder, "find_unattached_ebs_volumes", return_value=[]), \
                patch.object(finder, "find_unused_elastic_ips", return_value=[]), \
                patch.object(finder, "find_old_ebs_snapshots", return_value=[]) as snaps, \
                patch.object(finder, "find_idle_rds_instances", return_value=[]) as idle_rds:
            finder.scan_all(cpu_threshold=12.5, ec2_days=7, snapshot_days=90)

        idle_ec2.assert_called_once_with(12.5, 7)
        snaps.assert_called_once_with(90)
        idle_rds.assert_called_once_with(7)
