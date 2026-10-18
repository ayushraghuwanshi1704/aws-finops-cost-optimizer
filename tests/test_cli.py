"""
Unit tests for the CLI entrypoints in scripts/ (each module's main()).

These cover argparse wiring, option validation, output-format dispatch and
error exit codes — the layer the per-method tests do not reach. Every AWS
client is mocked; no credentials or network access are required.
"""
import csv
import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import commitment_coverage
import cost_analysis
import rightsizing_recommendations as rightsizing
import unused_resources


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ce_cost_response():
    """Minimal GetCostAndUsage response with two services."""
    return {
        "ResultsByTime": [{
            "TimePeriod": {"Start": "2026-09-01", "End": "2026-09-30"},
            "Groups": [
                {"Keys": ["Amazon EC2"],
                 "Metrics": {"UnblendedCost": {"Amount": "100.00", "Unit": "USD"}}},
                {"Keys": ["Amazon S3"],
                 "Metrics": {"UnblendedCost": {"Amount": "40.00", "Unit": "USD"}}},
            ],
        }]
    }


def _aws_clients():
    """Per-service mocks wired so a full scan legitimately finds nothing."""
    ec2 = MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = []
    ec2.describe_addresses.return_value = {"Addresses": []}
    ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}]}

    rds = MagicMock()
    rds.get_paginator.return_value.paginate.return_value = []

    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}

    return {"ec2": ec2, "rds": rds, "sts": sts, "cloudwatch": MagicMock()}


def _client_factory(clients):
    """boto3.client stand-in that routes each service name to its mock."""
    def client(service, **kwargs):
        return clients[service]
    return client


# ---------------------------------------------------------------------------
# cost_analysis.main
# ---------------------------------------------------------------------------

class TestCostAnalysisCli:

    def _analyzer_client(self):
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = _ce_cost_response()
        return ce

    def test_csv_output_written(self, tmp_path):
        out = tmp_path / "report.csv"
        ce = self._analyzer_client()
        with patch("cost_analysis.boto3.client", return_value=ce), \
                patch("sys.argv", ["cost_analysis.py", "--days", "30",
                                   "--output", str(out)]):
            cost_analysis.main()
        assert out.exists()
        header = out.read_text().splitlines()[0]
        assert header.startswith("PeriodStart,PeriodEnd,Dimension,Value,Cost")

    def test_json_output_written(self, tmp_path):
        out = tmp_path / "report.json"
        ce = self._analyzer_client()
        with patch("cost_analysis.boto3.client", return_value=ce), \
                patch("sys.argv", ["cost_analysis.py", "--output", str(out)]):
            cost_analysis.main()
        records = json.loads(out.read_text())
        assert records[0]["Value"] == "Amazon EC2"
        assert records[0]["Cost"] == 100.0

    def test_percentages_present_in_export(self, tmp_path):
        """calculate_percentage runs before export, so the column must exist."""
        out = tmp_path / "report.csv"
        ce = self._analyzer_client()
        with patch("cost_analysis.boto3.client", return_value=ce), \
                patch("sys.argv", ["cost_analysis.py", "--output", str(out)]):
            cost_analysis.main()
        assert "Percentage" in out.read_text().splitlines()[0]

    def test_unknown_output_extension_exits_1(self, tmp_path):
        out = tmp_path / "report.txt"
        ce = self._analyzer_client()
        with patch("cost_analysis.boto3.client", return_value=ce), \
                patch("sys.argv", ["cost_analysis.py", "--output", str(out)]), \
                pytest.raises(SystemExit) as excinfo:
            cost_analysis.main()
        assert excinfo.value.code == 1

    def test_invalid_group_by_rejected(self):
        """argparse restricts --group-by to the implemented dimensions."""
        with patch("cost_analysis.boto3.client", return_value=MagicMock()), \
                patch("sys.argv", ["cost_analysis.py", "--group-by", "COST_TYPE"]), \
                pytest.raises(SystemExit):
            cost_analysis.main()

    def test_no_output_file_still_prints_summary(self, capsys):
        ce = self._analyzer_client()
        with patch("cost_analysis.boto3.client", return_value=ce), \
                patch("sys.argv", ["cost_analysis.py"]):
            cost_analysis.main()
        assert "AWS Cost Analysis Summary" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cost_analysis.main -- demo mode
# ---------------------------------------------------------------------------

class TestCostAnalysisDemoCli:
    """--demo must produce the same report shape with boto3 never touched: the
    patch target is the module attribute itself, so even constructing a client
    would be recorded."""

    def _run(self, argv):
        with patch("cost_analysis.boto3") as boto3_mock, \
                patch("sys.argv", argv):
            cost_analysis.main()
        return boto3_mock

    def test_demo_prints_not_live_billing_warning(self, capsys):
        self._run(["cost_analysis.py", "--demo"])
        out = capsys.readouterr().out
        assert "DEMO DATA" in out
        assert "NOT LIVE AWS BILLING" in out
        assert "Amazon EC2" in out
        assert "$8,397.15" in out

    def test_demo_makes_no_aws_calls(self):
        """Requirement: demo mode must not create a client or reach the API."""
        boto3_mock = self._run(["cost_analysis.py", "--demo"])
        boto3_mock.client.assert_not_called()

    def test_demo_csv_export_is_labelled_and_totalled(self, tmp_path):
        out = tmp_path / "demo.csv"
        self._run(["cost_analysis.py", "--demo", "--output", str(out)])
        with open(out, newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 6
        assert "Source" in rows[0]
        assert {r["Source"] for r in rows} == {"DEMO DATA — NOT LIVE AWS BILLING"}
        total = sum(float(r["Cost"]) for r in rows)
        assert total == pytest.approx(8397.15, abs=0.01)
        assert sum(float(r["Percentage"]) for r in rows) == pytest.approx(100.0, abs=0.1)

    def test_demo_json_export_carries_percentage_and_source(self, tmp_path):
        out = tmp_path / "demo.json"
        self._run(["cost_analysis.py", "--demo", "--output", str(out)])
        records = json.loads(out.read_text())
        assert [r["Value"] for r in records] == [
            "Amazon EC2", "Amazon RDS", "AWS Data Transfer",
            "AWS Lambda", "Amazon S3", "Amazon CloudWatch",
        ]
        assert records[0]["Percentage"] == pytest.approx(57.41, abs=0.01)
        assert all(r["Source"] == "DEMO DATA — NOT LIVE AWS BILLING" for r in records)

    def test_demo_days_scales_the_sample_costs(self, tmp_path, capsys):
        out = tmp_path / "demo.json"
        self._run(["cost_analysis.py", "--demo", "--days", "15", "--output", str(out)])
        records = json.loads(out.read_text())
        total = sum(r["Cost"] for r in records)
        assert total == pytest.approx(8397.15 / 2, abs=0.05)
        assert "last 15 days" in capsys.readouterr().out

    def test_demo_rejects_unsupported_group_by(self, capsys):
        """Sample data only exists for SERVICE; silently ignoring --group-by
        would label a REGION report as if it were regional spend."""
        with patch("cost_analysis.boto3"), \
                patch("sys.argv", ["cost_analysis.py", "--demo", "--group-by",
                                   "REGION"]), \
                pytest.raises(SystemExit) as excinfo:
            cost_analysis.main()
        assert excinfo.value.code == 2
        assert "--group-by SERVICE only" in capsys.readouterr().err

    def test_demo_output_extension_rule_is_unchanged(self, tmp_path):
        out = tmp_path / "demo.txt"
        with pytest.raises(SystemExit) as excinfo:
            self._run(["cost_analysis.py", "--demo", "--output", str(out)])
        assert excinfo.value.code == 1
        assert not out.exists()


# ---------------------------------------------------------------------------
# unused_resources.main
# ---------------------------------------------------------------------------

class TestUnusedResourcesCli:

    def test_requires_region_or_all_regions(self, tmp_path, capsys):
        with patch("unused_resources.boto3.client", return_value=MagicMock()), \
                patch("sys.argv", ["unused_resources.py", "--output",
                                   str(tmp_path / "o.json")]), \
                pytest.raises(SystemExit) as excinfo:
            unused_resources.main()
        assert excinfo.value.code == 1
        assert "--region or --all-regions" in capsys.readouterr().err

    def test_region_option_is_passed_to_the_finder(self, tmp_path):
        out = tmp_path / "unused.json"
        clients = _aws_clients()
        with patch("unused_resources.boto3.client", side_effect=_client_factory(clients)), \
                patch("sys.argv", ["unused_resources.py", "--region", "eu-west-2",
                                   "--output", str(out)]):
            unused_resources.main()
        assert clients["ec2"].get_paginator.return_value.paginate.called
        assert json.loads(out.read_text())["Summary"]["TotalFindings"] == 0

    def test_region_overrides_all_regions(self, tmp_path):
        """--region wins when both are given; describe_regions must not be called."""
        out = tmp_path / "unused.json"
        clients = _aws_clients()
        with patch("unused_resources.boto3.client", side_effect=_client_factory(clients)), \
                patch("sys.argv", ["unused_resources.py", "--region", "us-east-1",
                                   "--all-regions", "--output", str(out)]):
            unused_resources.main()
        clients["ec2"].describe_regions.assert_not_called()

    def test_all_regions_expands_every_region(self, tmp_path):
        out = tmp_path / "unused.json"
        clients = _aws_clients()
        clients["ec2"].describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "ap-south-1"}]
        }
        with patch("unused_resources.boto3.client", side_effect=_client_factory(clients)), \
                patch("unused_resources.UnusedResourceFinder.scan_all",
                      return_value={k: [] for k in [
                          "IdleEC2Instances", "UnattachedEBSVolumes",
                          "UnusedElasticIPs", "OldEBSSnapshots",
                          "IdleRDSInstances"]}) as scan_all, \
                patch("sys.argv", ["unused_resources.py", "--all-regions",
                                   "--output", str(out)]):
            unused_resources.main()
        assert scan_all.call_count == 2

    def test_one_region_failure_does_not_abort_the_scan(self, tmp_path):
        """A broken region is reported and skipped; the remaining region still
        produces output. The cost tool must not lose the whole run to one region."""
        out = tmp_path / "unused.json"
        clients = _aws_clients()
        clients["ec2"].describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "eu-west-1"}]
        }
        denied = ClientError({"Error": {"Code": "UnauthorizedOperation"}},
                             "DescribeInstances")
        findings = {
            "IdleEC2Instances": [],
            "UnattachedEBSVolumes": [{
                "VolumeId": "vol-1", "SizeGiB": 100, "VolumeType": "gp3",
                "EstimatedMonthlySavingsUSD": 8.0,
            }],
            "UnusedElasticIPs": [], "OldEBSSnapshots": [], "IdleRDSInstances": [],
        }
        with patch("unused_resources.boto3.client",
                   side_effect=_client_factory(clients)), \
                patch("unused_resources.UnusedResourceFinder.scan_all",
                      side_effect=[denied, findings]), \
                patch("sys.argv", ["unused_resources.py", "--all-regions",
                                   "--output", str(out)]):
            unused_resources.main()
        summary = json.loads(out.read_text())["Summary"]
        assert summary["TotalFindings"] == 1
        assert summary["EstimatedMonthlySavingsUSD"] == 8.0

    def test_unwritable_output_exits_1(self, tmp_path):
        clients = _aws_clients()
        out = tmp_path / "missing-dir" / "unused.json"
        with patch("unused_resources.boto3.client", side_effect=_client_factory(clients)), \
                patch("sys.argv", ["unused_resources.py", "--region", "us-east-1",
                                   "--output", str(out)]), \
                pytest.raises(SystemExit) as excinfo:
            unused_resources.main()
        assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# rightsizing_recommendations.main
# ---------------------------------------------------------------------------

class TestRightSizingCli:

    def test_writes_json_and_reports_count(self, tmp_path, capsys):
        out = tmp_path / "recs.json"
        clients = _aws_clients()
        with patch("rightsizing_recommendations.boto3.client",
                   side_effect=_client_factory(clients)), \
                patch("sys.argv", ["rightsizing_recommendations.py",
                                   "--region", "eu-west-1", "--output", str(out)]):
            rightsizing.main()
        assert json.loads(out.read_text()) == []
        assert "recommendation(s) written" in capsys.readouterr().out

    def test_thresholds_forwarded_to_get_recommendations(self, tmp_path):
        out = tmp_path / "recs.json"
        clients = _aws_clients()
        with patch("rightsizing_recommendations.boto3.client",
                   side_effect=_client_factory(clients)), \
                patch("rightsizing_recommendations.RightSizingRecommender"
                      ".get_recommendations", return_value=[]) as get_recs, \
                patch("sys.argv", ["rightsizing_recommendations.py", "--region",
                                   "us-east-1", "--days", "30", "--cpu-threshold",
                                   "50", "--mem-threshold", "25", "--output", str(out)]):
            rightsizing.main()
        assert get_recs.call_args.kwargs == {
            "days": 30, "cpu_threshold": 50.0, "mem_threshold": 25.0,
        }

    def test_region_is_required(self, tmp_path):
        out = tmp_path / "recs.json"
        with patch("rightsizing_recommendations.boto3.client", return_value=MagicMock()), \
                patch("sys.argv", ["rightsizing_recommendations.py", "--output", str(out)]), \
                pytest.raises(SystemExit):
            rightsizing.main()

    def test_unwritable_output_exits_1(self, tmp_path):
        clients = _aws_clients()
        out = tmp_path / "missing-dir" / "recs.json"
        with patch("rightsizing_recommendations.boto3.client",
                   side_effect=_client_factory(clients)), \
                patch("sys.argv", ["rightsizing_recommendations.py", "--region",
                                   "us-east-1", "--output", str(out)]), \
                pytest.raises(SystemExit) as excinfo:
            rightsizing.main()
        assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# commitment_coverage.main
# ---------------------------------------------------------------------------

MOCK_REPORT = {
    "AnalysisPeriodDays": 30,
    "SavingsPlans": {"Utilization": None, "Coverage": None},
    "ReservedInstances": {"Utilization": None, "Coverage": None},
    "Observations": ["No Savings Plans or Reserved Instances found."],
}


class TestCommitmentCoverageCli:

    @pytest.fixture(autouse=True)
    def _no_aws_client(self):
        """CommitmentAnalyzer builds a Cost Explorer client in __init__; patching
        it here keeps these tests from reading host AWS config or credentials."""
        with patch("commitment_coverage.boto3.client", return_value=MagicMock()):
            yield

    def test_report_printed_to_stdout(self, capsys):
        """Without --output the report is still emitted for a human to read."""
        with patch("commitment_coverage.CommitmentAnalyzer.build_report",
                   return_value=MOCK_REPORT), \
                patch("sys.argv", ["commitment_coverage.py", "--days", "30"]):
            commitment_coverage.main()
        out = capsys.readouterr().out
        printed = json.loads(out[out.index("{"):])
        assert printed["AnalysisPeriodDays"] == 30
        assert printed["SavingsPlans"]["Utilization"] is None

    def test_output_file_written(self, tmp_path):
        out = tmp_path / "commitments.json"
        with patch("commitment_coverage.CommitmentAnalyzer.build_report",
                   return_value=MOCK_REPORT), \
                patch("sys.argv", ["commitment_coverage.py", "--output", str(out)]):
            commitment_coverage.main()
        assert json.loads(out.read_text())["AnalysisPeriodDays"] == 30

    def test_client_error_exits_1(self):
        """A non-DataUnavailable CostExplorer failure must fail loudly."""
        error = ClientError({"Error": {"Code": "AccessDeniedException"}},
                            "GetSavingsPlansUtilization")
        with patch("commitment_coverage.CommitmentAnalyzer.build_report",
                   side_effect=error), \
                patch("sys.argv", ["commitment_coverage.py"]), \
                pytest.raises(SystemExit) as excinfo:
            commitment_coverage.main()
        assert excinfo.value.code == 1

    def test_unwritable_output_exits_1(self, tmp_path):
        out = tmp_path / "missing-dir" / "commitments.json"
        with patch("commitment_coverage.CommitmentAnalyzer.build_report",
                   return_value=MOCK_REPORT), \
                patch("sys.argv", ["commitment_coverage.py", "--output", str(out)]), \
                pytest.raises(SystemExit) as excinfo:
            commitment_coverage.main()
        assert excinfo.value.code == 1
