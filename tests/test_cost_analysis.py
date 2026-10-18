"""
Unit tests for cost_analysis.py
"""
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cost_analysis import (
    DEMO_SAMPLE_MONTHLY_COSTS,
    DEMO_WARNING,
    CostAnalyzer,
    demo_cost_records,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_CE_RESPONSE = {
    "ResultsByTime": [
        {
            "TimePeriod": {"Start": "2026-02-01", "End": "2026-03-01"},
            "Groups": [
                {
                    "Keys": ["Amazon EC2"],
                    "Metrics": {"UnblendedCost": {"Amount": "1500.50", "Unit": "USD"}},
                },
                {
                    "Keys": ["Amazon S3"],
                    "Metrics": {"UnblendedCost": {"Amount": "250.00", "Unit": "USD"}},
                },
                {
                    "Keys": ["AWS Lambda"],
                    "Metrics": {"UnblendedCost": {"Amount": "12.75", "Unit": "USD"}},
                },
            ],
        }
    ],
    "ResponseMetadata": {},
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCostAnalyzer:

    def setup_method(self):
        # boto3 is patched so construction never reads host AWS config or
        # credentials; the client is replaced by a mock immediately below.
        with patch("cost_analysis.boto3"):
            self.analyzer = CostAnalyzer(region="us-east-1")
        self.analyzer.client = MagicMock()
        self.analyzer.client.get_cost_and_usage.return_value = MOCK_CE_RESPONSE

    def test_analyze_costs_returns_sorted_list(self):
        records = self.analyzer.analyze_costs(days=30, group_by_dimension="SERVICE")
        assert isinstance(records, list)
        assert len(records) == 3
        # Should be sorted by cost descending
        costs = [r["Cost"] for r in records]
        assert costs == sorted(costs, reverse=True)

    def test_analyze_costs_correct_values(self):
        records = self.analyzer.analyze_costs(days=30)
        top = records[0]
        assert top["Value"] == "Amazon EC2"
        assert top["Cost"] == 1500.50
        assert top["Currency"] == "USD"

    def test_pagination_merges_all_pages(self):
        """Large accounts paginate via NextPageToken — all pages must be read."""
        page1 = {
            "ResultsByTime": [{
                "TimePeriod": {"Start": "2026-02-01", "End": "2026-03-01"},
                "Groups": [{
                    "Keys": ["Amazon EC2"],
                    "Metrics": {"UnblendedCost": {"Amount": "100.00", "Unit": "USD"}},
                }],
            }],
            "NextPageToken": "token-1",
        }
        page2 = {
            "ResultsByTime": [{
                "TimePeriod": {"Start": "2026-03-01", "End": "2026-04-01"},
                "Groups": [{
                    "Keys": ["Amazon S3"],
                    "Metrics": {"UnblendedCost": {"Amount": "50.00", "Unit": "USD"}},
                }],
            }],
        }
        self.analyzer.client.get_cost_and_usage.side_effect = [page1, page2]
        records = self.analyzer.analyze_costs(days=60)
        assert {r["Value"] for r in records} == {"Amazon EC2", "Amazon S3"}
        assert self.analyzer.client.get_cost_and_usage.call_count == 2
        second_call = self.analyzer.client.get_cost_and_usage.call_args_list[1]
        assert second_call.kwargs["NextPageToken"] == "token-1"

    def test_multi_period_costs_aggregate_per_service(self):
        """A window spanning two calendar months yields one row per service, summed."""
        response = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-02-15", "End": "2026-03-01"},
                    "Groups": [{
                        "Keys": ["Amazon EC2"],
                        "Metrics": {"UnblendedCost": {"Amount": "100.00", "Unit": "USD"}},
                    }],
                },
                {
                    "TimePeriod": {"Start": "2026-03-01", "End": "2026-03-15"},
                    "Groups": [{
                        "Keys": ["Amazon EC2"],
                        "Metrics": {"UnblendedCost": {"Amount": "150.00", "Unit": "USD"}},
                    }],
                },
            ]
        }
        self.analyzer.client.get_cost_and_usage.return_value = response
        records = self.analyzer.analyze_costs(days=30)
        assert len(records) == 1
        assert records[0]["Cost"] == 250.00

    def test_calculate_total_cost(self):
        records = self.analyzer.analyze_costs(days=30)
        total = self.analyzer.calculate_total_cost(records)
        assert abs(total - 1763.25) < 0.01

    def test_calculate_percentage(self):
        records = self.analyzer.analyze_costs(days=30)
        records = self.analyzer.calculate_percentage(records)
        percentages = [r["Percentage"] for r in records]
        assert abs(sum(percentages) - 100.0) < 0.1
        assert all(0 <= p <= 100 for p in percentages)

    def test_calculate_percentage_zero_total(self):
        """Should return records unchanged when total cost is zero."""
        records = [{"Cost": 0.0}, {"Cost": 0.0}]
        result = self.analyzer.calculate_percentage(records)
        assert result == records

    def test_export_to_csv(self, tmp_path):
        records = self.analyzer.analyze_costs(days=30)
        records = self.analyzer.calculate_percentage(records)
        output = str(tmp_path / "test_report.csv")
        self.analyzer.export_to_csv(records, output)
        import csv
        with open(output) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 3
        assert rows[0]["Value"] == "Amazon EC2"

    def test_export_to_json(self, tmp_path):
        import json
        records = self.analyzer.analyze_costs(days=30)
        output = str(tmp_path / "test_report.json")
        self.analyzer.export_to_json(records, output)
        with open(output) as f:
            data = json.load(f)
        assert len(data) == 3

    def test_print_summary_no_crash(self, capsys):
        records = self.analyzer.analyze_costs(days=30)
        records = self.analyzer.calculate_percentage(records)
        self.analyzer.print_summary(records, top_n=2)
        captured = capsys.readouterr()
        assert "Amazon EC2" in captured.out
        assert "1,500.50" in captured.out

    def test_print_summary_empty(self, capsys):
        self.analyzer.print_summary([], top_n=5)
        captured = capsys.readouterr()
        assert "No cost data" in captured.out


class TestFailurePaths:
    """Error handling that decides whether a run fails loudly or silently."""

    def setup_method(self):
        with patch("cost_analysis.boto3"):
            self.analyzer = CostAnalyzer(region="us-east-1")
        self.analyzer.client = MagicMock()

    def test_query_error_exits_nonzero(self, capsys):
        """A Cost Explorer failure must exit 1, not return partial data as if
        the account simply had no spend."""
        self.analyzer.client.get_cost_and_usage.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "GetCostAndUsage"
        )
        with pytest.raises(SystemExit) as excinfo:
            self.analyzer.get_cost_and_usage("2026-02-01", "2026-03-01")
        assert excinfo.value.code == 1
        assert "Error querying Cost Explorer" in capsys.readouterr().err

    def test_export_to_csv_empty_input_writes_no_file(self, tmp_path):
        output = tmp_path / "report.csv"
        self.analyzer.export_to_csv([], str(output))
        assert not output.exists()

    def test_export_to_json_empty_input_still_writes(self, tmp_path):
        """Asymmetric by design: only the CSV writer guards against empty
        input, so the JSON path writes an empty array."""
        output = tmp_path / "report.json"
        self.analyzer.export_to_json([], str(output))
        assert json.loads(output.read_text()) == []

    def test_export_to_csv_write_failure_exits_nonzero(self, tmp_path):
        output = tmp_path / "no-such-dir" / "report.csv"
        with pytest.raises(SystemExit) as excinfo:
            self.analyzer.export_to_csv([{"Value": "Amazon EC2", "Cost": 1.0}], str(output))
        assert excinfo.value.code == 1

    def test_export_to_json_write_failure_exits_nonzero(self, tmp_path):
        output = tmp_path / "no-such-dir" / "report.json"
        with pytest.raises(SystemExit) as excinfo:
            self.analyzer.export_to_json([{"Value": "Amazon EC2"}], str(output))
        assert excinfo.value.code == 1


class TestDemoMode:
    """--demo formats labelled sample data through the same pipeline as live
    data, so the record shape must stay compatible."""

    def _analyzer(self):
        """A live analyzer whose Cost Explorer client is a mock, so construction
        never reads host AWS config."""
        with patch("cost_analysis.boto3"):
            analyzer = CostAnalyzer(region="us-east-1")
        analyzer.client = MagicMock()
        analyzer.client.get_cost_and_usage.return_value = MOCK_CE_RESPONSE
        return analyzer

    def _live_record_keys(self):
        return set(self._analyzer().analyze_costs(days=30)[0].keys())

    def test_demo_records_extend_the_live_record_shape(self):
        """Demo adds only the Source label — every field the summary, the
        percentage maths and the CSV writer depend on is identical."""
        assert set(demo_cost_records(days=30)[0].keys()) - self._live_record_keys() \
            == {"Source"}

    def test_every_demo_record_is_labelled_as_sample_data(self):
        records = demo_cost_records(days=30)
        assert records, "demo data must never be empty"
        for record in records:
            assert record["Source"] == DEMO_WARNING
            assert record["Dimension"] == "SERVICE"
            assert record["Currency"] == "USD"

    def test_demo_covers_the_expected_services(self):
        values = {r["Value"] for r in demo_cost_records(days=30)}
        assert values == {
            "Amazon EC2", "Amazon RDS", "AWS Data Transfer",
            "AWS Lambda", "Amazon S3", "Amazon CloudWatch",
        }

    def test_demo_totals_at_the_monthly_baseline(self):
        records = demo_cost_records(days=30)
        costs = [r["Cost"] for r in records]
        assert costs == sorted(costs, reverse=True)
        analyzer = CostAnalyzer(region="us-east-1", demo=True)
        assert abs(analyzer.calculate_total_cost(records) - 8397.15) < 0.01

    def test_demo_costs_scale_with_the_requested_window(self):
        """A 7-day report must not show a 30-day bill against a 7-day period."""
        monthly = {r["Value"]: r["Cost"] for r in demo_cost_records(days=30)}
        half = {r["Value"]: r["Cost"] for r in demo_cost_records(days=15)}
        for service, amount in monthly.items():
            assert half[service] == pytest.approx(amount / 2, abs=0.01)

    def test_demo_window_matches_the_requested_days(self):
        records = demo_cost_records(days=30)
        start = datetime.strptime(records[0]["PeriodStart"], "%Y-%m-%d").date()
        end = datetime.strptime(records[0]["PeriodEnd"], "%Y-%m-%d").date()
        assert (end - start).days == 30

    def test_demo_records_reuse_the_shared_percentage_helper(self):
        records = CostAnalyzer(region="us-east-1", demo=True).calculate_percentage(
            demo_cost_records(days=30)
        )
        assert abs(sum(r["Percentage"] for r in records) - 100.0) < 0.1

    def test_demo_analyzer_creates_no_cost_explorer_client(self):
        """The whole point of --demo: no client, therefore no credentials and
        no Cost Explorer access."""
        with patch("cost_analysis.boto3") as boto3_mock:
            analyzer = CostAnalyzer(region="us-east-1", demo=True)
        boto3_mock.client.assert_not_called()
        assert analyzer.client is None

    def test_live_analyzer_still_creates_a_regional_client(self):
        """Without --demo the live path is unchanged."""
        with patch("cost_analysis.boto3") as boto3_mock:
            CostAnalyzer(region="ap-south-1")
        boto3_mock.client.assert_called_once_with("ce", region_name="ap-south-1")

    def test_demo_summary_prints_the_not_live_billing_warning(self, capsys):
        analyzer = CostAnalyzer(region="us-east-1", demo=True)
        analyzer.print_summary(demo_cost_records(days=30), warning=DEMO_WARNING)
        captured = capsys.readouterr().out
        assert DEMO_WARNING in captured
        assert "8,397.15" in captured
        for service in DEMO_SAMPLE_MONTHLY_COSTS:
            assert service in captured

    def test_live_summary_has_no_demo_warning(self, capsys):
        """The banner is opt-in so a real billing report is never decorated."""
        analyzer = self._analyzer()
        analyzer.print_summary(analyzer.analyze_costs(days=30))
        assert "DEMO" not in capsys.readouterr().out
