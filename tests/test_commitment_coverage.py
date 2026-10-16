"""
Unit tests for commitment_coverage.py
"""
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from commitment_coverage import CommitmentAnalyzer


def _data_unavailable(operation):
    return ClientError({"Error": {"Code": "DataUnavailableException"}}, operation)


def _analyzer():
    a = CommitmentAnalyzer.__new__(CommitmentAnalyzer)
    a.client = MagicMock()
    return a


class TestSavingsPlansUtilization:

    def test_parses_totals(self):
        analyzer = _analyzer()
        analyzer.client.get_savings_plans_utilization.return_value = {
            "Total": {
                "Utilization": {
                    "UtilizationPercentage": "97.5",
                    "UnusedCommitment": "12.30",
                },
                "Savings": {"NetSavings": "450.00"},
            }
        }
        result = analyzer.savings_plans_utilization(days=30)
        assert result == {
            "UtilizationPercentage": 97.5,
            "UnusedCommitmentUSD": 12.30,
            "NetSavingsUSD": 450.00,
        }

    def test_no_savings_plans_returns_none(self):
        analyzer = _analyzer()
        analyzer.client.get_savings_plans_utilization.side_effect = _data_unavailable(
            "GetSavingsPlansUtilization"
        )
        assert analyzer.savings_plans_utilization(days=30) is None

    def test_other_client_errors_propagate(self):
        analyzer = _analyzer()
        analyzer.client.get_savings_plans_utilization.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "GetSavingsPlansUtilization"
        )
        try:
            analyzer.savings_plans_utilization(days=30)
            assert False, "expected ClientError"
        except ClientError:
            pass


class TestSavingsPlansCoverage:

    def test_coverage_is_weighted_by_eligible_spend(self):
        """A near-empty month at 100% must not mask a high-spend month at 0%
        (an arithmetic mean of the monthly percentages would report 50%)."""
        analyzer = _analyzer()
        analyzer.client.get_savings_plans_coverage.return_value = {
            "SavingsPlansCoverages": [
                {"Coverage": {"CoveragePercentage": "100.0", "TotalCost": "100",
                              "SpendCoveredBySavingsPlans": "100", "OnDemandCost": "0"}},
                {"Coverage": {"CoveragePercentage": "0.0", "TotalCost": "900",
                              "SpendCoveredBySavingsPlans": "0", "OnDemandCost": "900"}},
            ]
        }
        result = analyzer.savings_plans_coverage(days=60)
        assert result["CoveragePercentage"] == 10.0
        assert result["UncoveredOnDemandCostUSD"] == 900.0

    def test_empty_response_returns_none(self):
        analyzer = _analyzer()
        analyzer.client.get_savings_plans_coverage.return_value = {
            "SavingsPlansCoverages": []
        }
        assert analyzer.savings_plans_coverage(days=30) is None

    def test_zero_eligible_spend_returns_none(self):
        analyzer = _analyzer()
        analyzer.client.get_savings_plans_coverage.return_value = {
            "SavingsPlansCoverages": [
                {"Coverage": {"CoveragePercentage": "0.0", "TotalCost": "0",
                              "SpendCoveredBySavingsPlans": "0", "OnDemandCost": "0"}},
            ]
        }
        assert analyzer.savings_plans_coverage(days=30) is None


class TestReservations:

    def test_parses_ri_utilization(self):
        analyzer = _analyzer()
        analyzer.client.get_reservation_utilization.return_value = {
            "Total": {
                "UtilizationPercentage": "88.2",
                "UnusedHours": "120",
                "NetRISavings": "300.50",
            }
        }
        result = analyzer.reservation_utilization(days=30)
        assert result["UtilizationPercentage"] == 88.2
        assert result["UnusedHours"] == 120.0

    def test_parses_ri_coverage(self):
        analyzer = _analyzer()
        analyzer.client.get_reservation_coverage.return_value = {
            "Total": {"CoverageHours": {
                "CoverageHoursPercentage": "72.0",
                "OnDemandHours": "500",
                "ReservedHours": "1300",
            }}
        }
        result = analyzer.reservation_coverage(days=30)
        assert result["CoverageHoursPercentage"] == 72.0

    def test_empty_totals_return_none(self):
        analyzer = _analyzer()
        analyzer.client.get_reservation_utilization.return_value = {"Total": {}}
        analyzer.client.get_reservation_coverage.return_value = {"Total": {}}
        assert analyzer.reservation_utilization(days=30) is None
        assert analyzer.reservation_coverage(days=30) is None


class TestDataUnavailableBecomesAbsentSection:
    """Every section must degrade to None rather than raise, because main()
    serialises the whole report straight to JSON."""

    def test_savings_plans_coverage(self):
        analyzer = _analyzer()
        analyzer.client.get_savings_plans_coverage.side_effect = _data_unavailable(
            "GetSavingsPlansCoverage"
        )
        assert analyzer.savings_plans_coverage(days=30) is None

    def test_reservation_utilization(self):
        analyzer = _analyzer()
        analyzer.client.get_reservation_utilization.side_effect = _data_unavailable(
            "GetReservationUtilization"
        )
        assert analyzer.reservation_utilization(days=30) is None

    def test_reservation_coverage(self):
        analyzer = _analyzer()
        analyzer.client.get_reservation_coverage.side_effect = _data_unavailable(
            "GetReservationCoverage"
        )
        assert analyzer.reservation_coverage(days=30) is None


class TestConstructor:

    def test_cost_explorer_client_region(self):
        """Cost Explorer is a global API served from us-east-1 — overriding it
        with a deployment region would break every call."""
        with patch("commitment_coverage.boto3.client") as client:
            analyzer = CommitmentAnalyzer()
        client.assert_called_once_with("ce", region_name="us-east-1")
        assert analyzer.client is client.return_value


class TestBuildReport:
    """The composition layer that feeds _observations was never exercised."""

    def test_report_shape_and_window(self):
        analyzer = _analyzer()
        sp_util = {"UtilizationPercentage": 99.0, "UnusedCommitmentUSD": 1.0}
        with patch.object(analyzer, "savings_plans_utilization",
                          return_value=sp_util) as util, \
                patch.object(analyzer, "savings_plans_coverage", return_value=None), \
                patch.object(analyzer, "reservation_utilization", return_value=None), \
                patch.object(analyzer, "reservation_coverage", return_value=None):
            report = analyzer.build_report(days=45)

        assert report["AnalysisPeriodDays"] == 45
        assert set(report["SavingsPlans"]) == {"Utilization", "Coverage"}
        assert set(report["ReservedInstances"]) == {"Utilization", "Coverage"}
        assert report["SavingsPlans"]["Utilization"] == sp_util
        util.assert_called_once_with(45)

    def test_observations_derived_from_assembled_report(self):
        analyzer = _analyzer()
        with patch.object(analyzer, "savings_plans_utilization", return_value=None), \
                patch.object(analyzer, "savings_plans_coverage", return_value=None), \
                patch.object(analyzer, "reservation_utilization", return_value=None), \
                patch.object(analyzer, "reservation_coverage", return_value=None):
            report = analyzer.build_report(days=30)

        assert any("No Savings Plans or Reserved Instances" in o
                   for o in report["Observations"])


class TestObservations:

    def test_low_utilization_flagged(self):
        report = {
            "SavingsPlans": {
                "Utilization": {"UtilizationPercentage": 80.0, "UnusedCommitmentUSD": 200.0},
                "Coverage": None,
            },
            "ReservedInstances": {"Utilization": None, "Coverage": None},
        }
        observations = CommitmentAnalyzer._observations(report)
        assert any("80.0%" in o for o in observations)

    def test_low_coverage_flagged(self):
        report = {
            "SavingsPlans": {
                "Utilization": None,
                "Coverage": {"CoveragePercentage": 42.0, "UncoveredOnDemandCostUSD": 900.0},
            },
            "ReservedInstances": {"Utilization": None, "Coverage": None},
        }
        observations = CommitmentAnalyzer._observations(report)
        assert any("42.0%" in o and "$900.0" in o for o in observations)

    def test_low_reservation_utilization_flagged(self):
        report = {
            "SavingsPlans": {"Utilization": None, "Coverage": None},
            "ReservedInstances": {
                "Utilization": {"UtilizationPercentage": 70.0, "UnusedHours": 40.0,
                                "NetRISavingsUSD": 10.0},
                "Coverage": None,
            },
        }
        observations = CommitmentAnalyzer._observations(report)
        assert any("70.0%" in o and "40.0 unused hours" in o for o in observations)

    def test_no_commitments_recommends_modelling(self):
        report = {
            "SavingsPlans": {"Utilization": None, "Coverage": None},
            "ReservedInstances": {"Utilization": None, "Coverage": None},
        }
        observations = CommitmentAnalyzer._observations(report)
        assert any("No Savings Plans or Reserved Instances" in o for o in observations)

    def test_healthy_report_says_so(self):
        report = {
            "SavingsPlans": {
                "Utilization": {"UtilizationPercentage": 99.0, "UnusedCommitmentUSD": 1.0},
                "Coverage": {"CoveragePercentage": 85.0, "UncoveredOnDemandCostUSD": 50.0},
            },
            "ReservedInstances": {
                "Utilization": {"UtilizationPercentage": 98.0, "UnusedHours": 2.0,
                                "NetRISavingsUSD": 100.0},
                "Coverage": {"CoverageHoursPercentage": 90.0, "OnDemandHours": 10.0,
                             "ReservedHours": 90.0},
            },
        }
        observations = CommitmentAnalyzer._observations(report)
        assert observations == [
            "Commitment utilization and coverage look healthy for this period."
        ]
