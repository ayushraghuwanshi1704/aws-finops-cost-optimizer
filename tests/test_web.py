"""
Tests for the dashboard web layer (web/app.py, web/data.py, web/live.py,
web/reports.py).

Route tests call dispatch() directly, so no socket is bound except in the single
server test. The demo tests patch every AWS-facing class the data layer can
reach, which is how they prove a demo request makes no AWS call at all. The live
tests answer Cost Explorer from canned API responses, so they cover the same
record shapes the real service returns without leaving the machine.
"""
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date
from typing import Dict, List
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest
from botocore.exceptions import ClientError, MissingDependencyException, NoCredentialsError

from commitment_coverage import CommitmentAnalyzer
from cost_analysis import DEMO_WARNING
from web import app, data, demo_data, live, reports


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query(raw: str):
    return parse_qs(raw)


def _get_payload(query=""):
    status, headers, body = app.dispatch("GET", "/api/dashboard", _query(query))
    return status, headers, json.loads(body.decode("utf-8"))


def _ce_group(name: str, amount: str, unit: str = "USD") -> Dict:
    """One grouped metric, in the string form the Cost Explorer API returns."""
    return {"Keys": [name], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}


def _ce_period(start: str, end: str, groups: List[Dict] = None,
               total: str = None, estimated: bool = False) -> Dict:
    """One ResultsByTime entry, grouped or ungrouped, as the API would send it."""
    period = {"TimePeriod": {"Start": start, "End": end}, "Estimated": estimated}
    if groups is not None:
        period["Groups"] = groups
    if total is not None:
        period["Total"] = {"UnblendedCost": {"Amount": total, "Unit": "USD"}}
    return period


def _fake_ce(grouped: Dict[str, List[Dict]] = None, trend: List[Dict] = None,
             error: Exception = None) -> MagicMock:
    """
    A `ce` client stand-in that answers like Cost Explorer.

    Dispatching on GroupBy lets one fake serve the service, region and trend
    queries live_payload() makes, and every request is recorded so a test can
    read the exact TimePeriod and Granularity that were sent.
    """
    grouped = grouped or {}
    calls: List[Dict] = []
    client = MagicMock()
    client.calls = calls

    def respond(**params):
        calls.append(params)
        if error is not None:
            raise error
        group_by = params.get("GroupBy")
        if not group_by:
            return {"ResultsByTime": list(trend or [])}
        return {"ResultsByTime": list(grouped.get(group_by[0]["Key"], []))}

    client.get_cost_and_usage.side_effect = respond
    return client


def _empty_live_sections(finder, recommender, commitments) -> None:
    """Point the three scripts-based collectors at 'nothing found' results."""
    finder.return_value.scan_all.return_value = {key: [] for key in data.WASTE_KEYS}
    recommender.return_value.get_recommendations.return_value = []
    commitments.return_value.build_report.return_value = {
        "AnalysisPeriodDays": 30,
        "SavingsPlans": {"Utilization": None, "Coverage": None},
        "ReservedInstances": {"Utilization": None, "Coverage": None},
        "Observations": [],
    }


def _commitment_report(days: int = 30) -> Dict:
    """A filled-in report, shaped exactly like CommitmentAnalyzer.build_report()."""
    return {
        "AnalysisPeriodDays": days,
        "SavingsPlans": {
            "Utilization": {"UtilizationPercentage": 91.0, "UnusedCommitmentUSD": 4.0,
                            "NetSavingsUSD": 12.0},
            "Coverage": {"CoveragePercentage": 40.0, "UncoveredOnDemandCostUSD": 90.0},
        },
        "ReservedInstances": {
            "Utilization": {"UtilizationPercentage": 50.0, "UnusedHours": 6.0,
                            "NetRISavingsUSD": 3.0},
            "Coverage": {"CoverageHoursPercentage": 20.0, "OnDemandHours": 80.0,
                         "ReservedHours": 20.0},
        },
        "Observations": ["RI coverage is low."],
    }


@contextmanager
def _live_mode(grouped=None, trend=None, query_error=None, client_error=None,
               commitment_report=None):
    """
    Run live_payload() with every AWS entry point faked.

    Cost Explorer spend comes from `web.live`, and waste, right-sizing and
    commitments from the scripts classes, so all four are stubbed here: a live
    test must stay hermetic even on a machine with working credentials.
    """
    if client_error is not None:
        client = None
        patcher = patch("web.live.make_client", side_effect=client_error)
    else:
        client = _fake_ce(grouped, trend, query_error)
        patcher = patch("web.live.make_client", return_value=client)

    with patcher, patch("web.data.UnusedResourceFinder") as finder, \
            patch("web.data.RightSizingRecommender") as recommender, \
            patch("web.data.CommitmentAnalyzer") as commitments:
        _empty_live_sections(finder, recommender, commitments)
        if commitment_report is not None:
            commitments.return_value.build_report.return_value = commitment_report
        yield client


@contextmanager
def _serve():
    """Run the real server on an ephemeral loopback port for one test."""
    server = app.create_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class TestRoutes:

    def test_index_serves_the_dashboard_shell(self):
        status, headers, body = app.dispatch("GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "FinOps Cost Optimizer" in body.decode("utf-8")
        # The page has no inline script, so a strict CSP is a real guarantee
        # rather than decoration.
        assert "Content-Security-Policy" in headers

    def test_static_assets_are_served_with_types(self):
        for name, expected in (("styles.css", "text/css"), ("app.js", "javascript")):
            status, headers, body = app.dispatch("GET", f"/static/{name}")
            assert status == 200, name
            assert expected in headers["Content-Type"], name
            assert len(body) > 200, name

    def test_static_path_traversal_is_refused(self):
        """Serving is an allowlist, not a normalised join, so ../ never resolves."""
        for path in ("/static/../data.py", "/static/../../scripts/cost_analysis.py",
                     "/static/", "/static/STYLES.CSS"):
            status, _, body = app.dispatch("GET", path)
            assert status == 404, path
            assert b"error" in body, path

    def test_unknown_path_and_post_are_rejected(self):
        assert app.dispatch("GET", "/api/nope")[0] == 404
        assert app.dispatch("POST", "/api/dashboard")[0] == 405

    def test_health_endpoint_reports_mode_and_day_filters(self):
        status, _, body = app.dispatch("GET", "/api/health")
        health = json.loads(body.decode("utf-8"))
        assert status == 200
        assert health["status"] == "ok"
        assert health["defaultMode"] == "demo"
        assert health["supportedDayFilters"] == [7, 30, 90]
        assert health["aws"]["apiCallsMade"] is False

    def test_health_never_leaks_credentials(self):
        """A resolved credential object must not be serialised into the response."""
        creds = MagicMock()
        creds.access_key = "AKIAIOSFODNN7EXAMPLE"
        creds.secret_key = "wJalrXUtnFEMISECRETVALUE"
        creds.token = "SESSION-TOKEN-SECRET"
        with patch("boto3.session.Session") as session:
            session.return_value.get_credentials.return_value = creds
            _, _, body = app.dispatch("GET", "/api/health")
        text = body.decode("utf-8")
        assert json.loads(text)["aws"]["credentials"] == "available"
        for secret in ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMISECRETVALUE", "SESSION-TOKEN-SECRET"):
            assert secret not in text

    def test_health_survives_a_broken_credential_provider(self):
        """The host profile can fail to resolve at all; health must still answer."""
        with patch("boto3.session.Session", side_effect=RuntimeError("provider is broken")):
            status, _, body = app.dispatch("GET", "/api/health")
        assert status == 200
        assert json.loads(body.decode("utf-8"))["aws"]["credentials"] == "error"

    def test_server_answers_over_http(self):
        """One real socket test: create_server and the handler wiring, not just dispatch()."""
        with _serve() as base:
            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
                assert response.status == 200
                assert json.loads(response.read())["status"] == "ok"
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(f"{base}/nope", timeout=5)
            assert err.value.code == 404

    def test_payload_failure_becomes_a_json_error_not_a_dead_server(self):
        """A raised exception in one request must not take the demo down."""
        with _serve() as base:
            with patch("web.app.build_payload", side_effect=RuntimeError("boom")):
                with pytest.raises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(f"{base}/api/dashboard?days=30", timeout=5)
                assert err.value.code == 500
                assert b"dashboard error" in err.value.read()
            # ...and the very next request still works.
            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as response:
                assert json.loads(response.read())["status"] == "ok"


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

class TestParameters:

    def test_day_filters_are_the_three_offered_windows(self):
        for days in (7, 30, 90):
            status, _, payload = _get_payload(f"days={days}&mode=demo")
            assert status == 200
            assert payload["meta"]["days"] == days
            assert payload["totals"]["PeriodDays"] == days

    def test_unsupported_days_fall_back_to_30(self):
        """A hand-typed 15 must not silently change the billing window."""
        for raw in ("15", "abc", "0", "365"):
            _, _, payload = _get_payload(f"days={raw}&mode=demo")
            assert payload["meta"]["days"] == 30, raw

    def test_unknown_mode_falls_back_to_demo(self):
        _, _, payload = _get_payload("mode=production&days=30")
        assert payload["meta"]["mode"] == "demo"

    def test_demo_only_server_refuses_live(self):
        params = app.parse_params(_query("mode=live"), allow_live=False)
        assert params["mode"] == "demo"
        assert params["liveRequested"] is True

    def test_region_is_validated_before_it_reaches_aws(self):
        assert app.parse_params(_query("region=ap-south-1"))["region"] == "ap-south-1"
        for bad in ("../etc", "US-EAST-1; rm -rf /", "ab", "a" * 40):
            params = app.parse_params(_query(f"region={bad}"))
            assert params["region"] == app.DEFAULT_REGION, bad


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

class TestDemoPayload:

    def test_demo_is_labelled_in_meta_and_every_cost_row(self):
        _, _, payload = _get_payload("days=30&mode=demo")
        assert payload["meta"]["warning"] == DEMO_WARNING
        assert payload["meta"]["mode"] == "demo"
        for record in payload["services"] + payload["regions"]:
            assert record["Source"] == DEMO_WARNING

    def test_demo_populates_every_section_without_errors(self):
        _, _, payload = _get_payload("days=30&mode=demo")
        assert payload["services"] and payload["regions"] and payload["trend"]
        assert payload["waste"]["findings"] and payload["rightsizing"]
        assert payload["commitments"]["SavingsPlans"]["Utilization"]["UtilizationPercentage"] == \
            pytest.approx(62.4)
        assert payload["errors"] == []

    def test_demo_service_and_region_totals_reconcile(self):
        _, _, payload = _get_payload("days=30&mode=demo")
        services = round(sum(r["Cost"] for r in payload["services"]), 2)
        regions = round(sum(r["Cost"] for r in payload["regions"]), 2)
        assert services == regions == payload["totals"]["TotalCostUSD"]

    def test_demo_percentages_sum_to_a_hundred(self):
        _, _, payload = _get_payload("days=90&mode=demo")
        for key in ("services", "regions"):
            assert abs(sum(r["Percentage"] for r in payload[key]) - 100.0) < 0.1, key

    def test_demo_trend_uses_the_periods_live_would_query(self):
        """
        The sample chart is built from the same period buckets as a real one.

        A fixed six-point script once made every filter look like a six-month
        history, so the buckets, labels and granularity are now checked against
        web.live for all three supported windows.
        """
        for days in data.SUPPORTED_DAYS:
            _, _, payload = _get_payload(f"days={days}&mode=demo")
            period = payload["meta"]["period"]
            trend = payload["trend"]
            assert len(trend) == len(live.period_buckets(period)), days
            assert [(p["PeriodStart"], p["PeriodEnd"]) for p in trend] == \
                live.period_buckets(period), days
            assert all(p["Granularity"] == period["Granularity"] for p in trend), days
            assert all(p["Source"] == DEMO_WARNING and p["Estimated"] is False
                       for p in trend), days
            assert round(sum(p["Cost"] for p in trend), 2) == \
                payload["totals"]["TotalCostUSD"], days

    def test_demo_trend_labels_match_the_granularity(self):
        _, _, short = _get_payload("days=7&mode=demo")
        _, _, long_ = _get_payload("days=90&mode=demo")
        assert [p["Label"] for p in short["trend"]][0] == short["meta"]["period"]["Start"][5:]
        assert short["meta"]["granularity"] == "DAILY" and len(short["trend"]) == 7
        assert long_["meta"]["granularity"] == "MONTHLY"
        assert all(len(p["Label"]) == 7 for p in long_["trend"])

    def test_demo_totals_are_derived_not_stored(self):
        """The run rate is normalised from the window, so 7d and 90d agree."""
        _, _, short = _get_payload("days=7&mode=demo")
        _, _, long_ = _get_payload("days=90&mode=demo")
        assert short["totals"]["TotalCostUSD"] < long_["totals"]["TotalCostUSD"]
        assert short["totals"]["MonthlyRunRateUSD"] == pytest.approx(
            long_["totals"]["MonthlyRunRateUSD"], abs=1.0)

    def test_demo_waste_uses_the_production_finding_shape(self):
        """build_summary() is the CLI's own function, so demo findings must fit it."""
        _, _, payload = _get_payload("days=30&mode=demo")
        summary = payload["waste"]["summary"]
        assert set(summary) >= {"TotalFindings", "FindingsByType",
                                "EstimatedMonthlySavingsUSD", "UnquantifiedFindings"}
        assert summary["TotalFindings"] == sum(
            len(v) for v in payload["waste"]["findings"].values())

    def test_demo_commitments_match_the_analyzer_report_shape(self):
        """
        The commitments panel reads keys out of CommitmentAnalyzer's report.

        Demo data once carried a flatter shape that rendered perfectly and hid
        the fact that live data would not, so the comparison is against the
        analyzer's own return values rather than against a second constant.
        """
        analyzer = CommitmentAnalyzer.__new__(CommitmentAnalyzer)
        client = MagicMock()
        client.get_savings_plans_utilization.return_value = {"Total": {
            "Utilization": {"UtilizationPercentage": "62", "UnusedCommitment": "18"},
            "Savings": {"NetSavings": "41"},
        }}
        client.get_savings_plans_coverage.return_value = {"SavingsPlansCoverages": [
            {"Coverage": {"SpendCoveredBySavingsPlans": "42", "TotalCost": "100",
                          "OnDemandCost": "58"}},
        ]}
        client.get_reservation_utilization.return_value = {"Total": {
            "UtilizationPercentage": "88", "UnusedHours": "5", "NetRISavings": "24",
        }}
        client.get_reservation_coverage.return_value = {"Total": {"CoverageHours": {
            "CoverageHoursPercentage": "33", "OnDemandHours": "73", "ReservedHours": "37",
        }}}
        analyzer.client = client
        analyzer._call = lambda description, fn, **kwargs: fn(**kwargs)
        live_report = {
            "SavingsPlans": {
                "Utilization": analyzer.savings_plans_utilization(30),
                "Coverage": analyzer.savings_plans_coverage(30),
            },
            "ReservedInstances": {
                "Utilization": analyzer.reservation_utilization(30),
                "Coverage": analyzer.reservation_coverage(30),
            },
        }

        _, _, payload = _get_payload("days=30&mode=demo")
        demo_report = payload["commitments"]
        for block in ("SavingsPlans", "ReservedInstances"):
            for key in ("Utilization", "Coverage"):
                live, demo = live_report[block][key], demo_report[block][key]
                assert isinstance(demo, dict), f"{block}.{key} must be a dict, like live"
                assert set(demo) == set(live), f"{block}.{key} keys differ from the analyzer"
                assert all(isinstance(v, (int, float)) for v in demo.values())

    def test_demo_request_makes_no_aws_calls(self):
        """The core demo guarantee: no client, no credentials, no Cost Explorer."""
        with patch("web.live.make_client") as ce_client, \
                patch("web.data.CommitmentAnalyzer") as commitments, \
                patch("web.data.UnusedResourceFinder") as finder, \
                patch("web.data.RightSizingRecommender") as recommender, \
                patch("cost_analysis.boto3") as ce_boto3, \
                patch("unused_resources.boto3") as ec2_boto3, \
                patch("rightsizing_recommendations.boto3") as rs_boto3, \
                patch("commitment_coverage.boto3") as cc_boto3:
            status, _, payload = _get_payload("days=30&mode=demo")
            assert status == 200
            for days in data.SUPPORTED_DAYS:
                _get_payload(f"days={days}&mode=demo")

        ce_client.assert_not_called()
        commitments.assert_not_called()
        finder.assert_not_called()
        recommender.assert_not_called()
        for module in (ce_boto3, ec2_boto3, rs_boto3, cc_boto3):
            module.client.assert_not_called()
        # The shared percentage helper is the CLI's own maths, held on a demo
        # analyzer so it needs no client. A non-None client here would mean a
        # demo request can reach AWS through it.
        assert data._PERCENTAGE_ENGINE.client is None
        assert payload["meta"]["mode"] == "demo"
        assert payload["meta"]["aws"]["apiCallsMade"] is False

    def test_demo_works_with_no_credentials_at_all(self):
        """Unresolvable credentials must not affect a demo request."""
        with patch("boto3.session.Session", side_effect=RuntimeError("no credentials")):
            status, _, payload = _get_payload("days=30&mode=demo")
        assert status == 200
        assert payload["meta"]["aws"]["credentials"] == "error"
        assert payload["services"], "demo data must be complete regardless of AWS state"


# ---------------------------------------------------------------------------
# Live mode honesty
# ---------------------------------------------------------------------------

class TestLivePayload:
    """What Live mode serves when Cost Explorer answers - and when it refuses."""

    def test_live_payload_never_falls_back_to_sample_data(self):
        """If the Cost Explorer client cannot be built the numbers must be empty."""
        with _live_mode(client_error=NoCredentialsError()):
            payload = data.live_payload(30, "us-east-1")
        assert payload["services"] == []
        assert payload["regions"] == []
        assert payload["trend"] == []
        assert payload["totals"]["TotalCostUSD"] == 0
        assert payload["meta"]["warning"] is None
        assert payload["errors"][0]["Section"] == "cost explorer client"
        assert "Demo" in payload["errors"][0]["Hint"]
        assert payload["errors"][0]["Code"] == "NoCredentialsError"
        text = json.dumps(payload)
        assert DEMO_WARNING not in text
        # The demo baseline total appearing anywhere here would mean sample
        # figures were being presented as live billing.
        assert f"{demo_data.monthly_sample_total():.2f}" not in text

    def test_live_mode_serves_what_cost_explorer_returns(self):
        """Grouped amounts are summed across periods and keep the API's own units."""
        grouped = {
            "SERVICE": [
                _ce_period("2000-01-01", "2000-02-01", groups=[
                    _ce_group("Amazon EC2", "700.50"), _ce_group("AWS Support", "-20.25")]),
                _ce_period("2000-02-01", "2000-03-01", groups=[
                    _ce_group("Amazon EC2", "500.00"), _ce_group("Amazon S3", "0.00"),
                    _ce_group("  ", "1.00")]),
            ],
            "REGION": [
                _ce_period("2000-01-01", "2000-03-01", groups=[
                    _ce_group("eu-west-1", "1180.25", unit="EUR")]),
            ],
        }
        trend = [
            _ce_period("2000-01-01", "2000-02-01", total="680.25"),
            _ce_period("2000-02-01", "2000-03-01", total="500.00", estimated=True),
        ]
        with _live_mode(grouped=grouped, trend=trend,
                        commitment_report=_commitment_report()) as client:
            payload = data.live_payload(30, "eu-west-1")

        assert payload["meta"]["mode"] == "live"
        assert payload["meta"]["warning"] is None
        assert payload["errors"] == []
        by_service = {row["Value"]: row for row in payload["services"]}
        assert by_service["Amazon EC2"]["Cost"] == 1200.50
        assert by_service["AWS Support"]["Cost"] == -20.25
        assert by_service["Amazon S3"]["Cost"] == 0.0
        # A blank dimension key is Cost Explorer's "other" bucket; it must be
        # named, not dropped, or the table silently disagrees with the total.
        assert by_service["Uncategorized"]["Cost"] == 1.0
        assert [row["Value"] for row in payload["services"]] == sorted(
            by_service, key=lambda name: -by_service[name]["Cost"])
        assert payload["regions"][0]["Currency"] == "EUR"
        assert payload["totals"]["Currency"] == "USD"
        assert payload["totals"]["TotalCostUSD"] == 1181.25
        # A credited line makes another service's share exceed 100%. That is the
        # arithmetic working correctly, so the payload shows it rather than
        # clamping it into a tidy chart.
        assert by_service["Amazon EC2"]["Percentage"] == pytest.approx(101.63)
        assert "Source" not in payload["services"][0]
        assert payload["commitments"]["Observations"] == ["RI coverage is low."]

        # Period labels are the window actually covered, not the one requested.
        period = payload["meta"]["period"]
        assert payload["services"][0]["PeriodStart"] == period["Start"]
        assert payload["services"][0]["PeriodEnd"] == period["EndInclusive"]

        dimensions = [call["GroupBy"][0]["Key"] for call in client.calls if "GroupBy" in call]
        assert dimensions == ["SERVICE", "REGION"]
        for call in client.calls:
            assert call["TimePeriod"] == {"Start": period["Start"],
                                          "End": period["EndExclusive"]}
            assert call["Granularity"] == period["Granularity"]
            assert call["Metrics"] == ["UnblendedCost"]

        # Trend points keep the API's own period edges and estimate flags.
        assert [point["Cost"] for point in payload["trend"]] == [680.25, 500.0]
        assert payload["trend"][1]["Estimated"] is True
        assert payload["trend"][0]["PeriodEnd"] == "2000-02-01"
        assert payload["trend"][0]["Label"] == payload["trend"][0]["PeriodStart"][5:]

    def test_a_refused_query_keeps_its_error_code_and_hint(self):
        """AccessDenied must read as a permissions problem, not as zero spend."""
        denied = ClientError(
            {"Error": {"Code": "AccessDeniedException",
                       "Message": "User is not authorized to perform ce:GetCostAndUsage"}},
            "GetCostAndUsage")
        with _live_mode(query_error=denied):
            payload = data.live_payload(7, "us-east-1")

        assert payload["services"] == [] and payload["trend"] == []
        failing = [e for e in payload["errors"] if e["Section"] in ("services", "regions", "trend")]
        assert len(failing) == 3
        assert {e["Code"] for e in failing} == {"AccessDeniedException"}
        assert all("ce:GetCostAndUsage" in e["Hint"] for e in failing)
        assert all("AccessDeniedException" in e["Message"] for e in failing)
        assert payload["meta"]["mode"] == "live"
        assert DEMO_WARNING not in json.dumps(payload)

    def test_an_empty_account_is_data_not_an_error(self):
        """ResultsByTime with nothing in it is a real answer: no spend, no failure."""
        with _live_mode(grouped={"SERVICE": [], "REGION": []}, trend=[]):
            payload = data.live_payload(7, "us-east-1")
        assert payload["services"] == [] and payload["regions"] == []
        assert payload["trend"] == []
        assert payload["errors"] == []
        assert payload["totals"]["TotalCostUSD"] == 0
        assert payload["totals"]["TopService"] is None

    def test_live_endpoint_marks_partial_data_as_multistatus(self):
        """Spend that arrived alongside a failed waste scan is still served."""
        grouped = {"SERVICE": [_ce_period("2000-01-01", "2000-02-01",
                                          groups=[_ce_group("Amazon EC2", "10.00")])]}
        with _live_mode(grouped=grouped, trend=[]), \
                patch("web.data.UnusedResourceFinder",
                      side_effect=SystemExit(1)):
            status, _, body = app.dispatch(
                "GET", "/api/dashboard", _query("mode=live&days=30"))
        payload = json.loads(body.decode("utf-8"))
        assert status == 207
        assert payload["services"][0]["Cost"] == 10.0
        assert payload["errors"][0]["Section"] == "waste"
        assert "Cost Explorer may be disabled" in payload["errors"][0]["Message"]

    def test_clean_live_run_is_served_as_200(self):
        grouped = {"SERVICE": [_ce_period("2000-01-01", "2000-02-01",
                                          groups=[_ce_group("Amazon EC2", "10.00")])]}
        with _live_mode(grouped=grouped, trend=[]):
            status, _, body = app.dispatch(
                "GET", "/api/dashboard", _query("mode=live&days=30"))
        assert status == 200
        assert json.loads(body.decode("utf-8"))["errors"] == []

    def test_live_export_has_no_demo_label(self):
        grouped = {"SERVICE": [_ce_period("2000-01-01", "2000-02-01",
                                          groups=[_ce_group("Amazon EC2", "10.00")])]}
        with _live_mode(grouped=grouped, trend=[]):
            _, _, body = app.dispatch(
                "GET", "/api/report/csv", _query("mode=live&days=30"))
        text = body.decode("utf-8")
        assert DEMO_WARNING not in text
        assert "Amazon EC2" in text
        assert len(text.splitlines()) == 2

    def test_a_zero_spend_live_run_is_reported_as_connected(self):
        """
        A real account can bill under a cent per service.

        That answer used to render as an empty, failed-looking page, so the
        payload has to state that the connection succeeded and the window
        simply has no material spend - the rows are present, only the money is
        small.
        """
        groups = [_ce_group("AWS Lambda", "0.0004217"),
                  _ce_group("AmazonS3", "-0.0000003422"),
                  _ce_group("Amazon CloudWatch", "0.0")]
        grouped = {"SERVICE": [_ce_period("2000-01-01", "2000-01-08", groups=groups)],
                   "REGION": [_ce_period("2000-01-01", "2000-01-08",
                                         groups=[_ce_group("us-east-1", "0.0004217")])]}
        trend = [_ce_period("2000-01-01", "2000-01-02", total="0.0004217"),
                 _ce_period("2000-01-02", "2000-01-03", total="0.0")]
        with _live_mode(grouped=grouped, trend=trend):
            status, _, body = app.dispatch(
                "GET", "/api/dashboard", _query("mode=live&days=7"))
        payload = json.loads(body.decode("utf-8"))

        assert status == 200
        assert payload["errors"] == []
        # The rows are served, not hidden behind an unavailable state.
        assert {row["Value"] for row in payload["services"]} == {
            "AWS Lambda", "AmazonS3", "Amazon CloudWatch"}
        assert payload["totals"]["TotalCostUSD"] == 0.0
        assert [point["Cost"] for point in payload["trend"]] == [0.0, 0.0]

        live_status = payload["meta"]["status"]
        assert live_status["State"] == "connected-zero"
        assert "connected" in live_status["Message"].lower()
        assert "no material spend in this period" in live_status["Message"]
        assert "unavailable" not in live_status["Message"].lower()
        assert "3 service line(s)" in live_status["Message"]
        assert DEMO_WARNING not in json.dumps(payload)


# ---------------------------------------------------------------------------
# The availability state the UI reads
# ---------------------------------------------------------------------------

class TestLiveStatus:

    def test_spend_above_a_cent_reads_as_plain_connected(self):
        services = [{"Value": "Amazon EC2", "Cost": 412.55, "Currency": "USD"}]
        status = data.status_for("live", [], services, 412.55)
        assert status["State"] == "connected"
        assert "no material spend" not in status["Message"]

    def test_a_zero_window_is_connected_data_not_an_outage(self):
        services = [{"Value": "AWS Lambda", "Cost": 0.0, "Currency": "USD"}]
        status = data.status_for("live", [], services, 0.0)
        assert status["State"] == "connected-zero"
        assert "unavailable" not in status["Message"].lower()

    def test_a_credit_only_window_is_still_material_spend(self):
        """A $50 credit is money the account moved; rounding never makes it nothing."""
        services = [{"Value": "AWS Support", "Cost": -50.0, "Currency": "USD"}]
        assert data.status_for("live", [], services, -50.0)["State"] == "connected"

    def test_a_non_usd_zero_is_described_in_the_billed_currency(self):
        services = [{"Value": "Amazon S3", "Cost": 0.0, "Currency": "EUR"}]
        message = data.status_for("live", [], services, 0.0)["Message"]
        assert "0.00 EUR" in message
        assert "$0.00" not in message

    def test_no_cost_rows_at_all_is_connected_and_empty(self):
        status = data.status_for("live", [], [], 0.0)
        assert status["State"] == "connected-empty"
        assert "no billable usage" in status["Message"]

    def test_any_error_downgrades_the_state(self):
        """Partial spend is real, but a green 'connected' banner would overstate it."""
        errors = [{"Section": "waste", "Code": "AccessDenied", "Message": "nope"}]
        services = [{"Value": "Amazon EC2", "Cost": 412.55, "Currency": "USD"}]
        status = data.status_for("live", errors, services, 412.55)
        assert status["State"] == "partial"
        assert "1 section(s) could not be read" in status["Message"]

    def test_no_spend_plus_errors_is_the_unavailable_state(self):
        """The genuine failure keeps the message the earlier stage was showing."""
        errors = [{"Section": "services", "Code": "UnrecognizedClientException",
                   "Message": "The security token included in the request is invalid"}]
        status = data.status_for("live", errors, [], 0.0)
        assert status["State"] == "unavailable"
        assert "not live data" in status["Message"]

    def test_demo_mode_is_never_reported_as_connected(self):
        """The status field must not become a way to label sample data as live."""
        services = [{"Value": "Amazon EC2", "Cost": 1200.0, "Currency": "USD"}]
        status = data.status_for("demo", [], services, 1200.0)
        assert status["State"] == "demo"
        assert "demo" in status["Message"].lower()
        assert "connected" not in status["Message"].lower()

    def test_demo_payload_carries_the_demo_status(self):
        payload = data.demo_payload(30)
        assert payload["meta"]["status"]["State"] == "demo"

    def test_a_refused_client_leaves_the_unavailable_status(self):
        with _live_mode(client_error=NoCredentialsError()):
            payload = data.live_payload(7, "us-east-1")
        assert payload["meta"]["status"]["State"] == "unavailable"
        assert payload["errors"]

    def test_partial_live_data_is_not_reported_as_connected(self):
        grouped = {"SERVICE": [_ce_period("2000-01-01", "2000-02-01",
                                          groups=[_ce_group("Amazon EC2", "10.00")])]}
        with _live_mode(grouped=grouped, trend=[]), \
                patch("web.data.CommitmentAnalyzer", side_effect=SystemExit(1)):
            payload = data.live_payload(30, "us-east-1")
        assert payload["meta"]["status"]["State"] == "partial"
        assert payload["services"][0]["Cost"] == 10.0


# ---------------------------------------------------------------------------
# The live Cost Explorer layer itself
# ---------------------------------------------------------------------------

class TestLiveCostExplorer:

    def test_window_excludes_the_day_cost_explorer_cannot_bill(self):
        period = live.window(7)
        assert period["Days"] == 7
        assert period["Start"] != period["EndExclusive"]
        assert period["EndInclusive"] < period["EndExclusive"]
        assert "exclusive" in period["Note"]
        assert period["EndInclusive"] in period["Note"]

    def test_query_sends_the_exclusive_end(self):
        """The API must receive End, not EndInclusive, or the last day is billed twice."""
        period = live.window(30)
        client = _fake_ce(trend=[])
        live.query(client, period, "DAILY")
        assert client.calls[0]["TimePeriod"] == {
            "Start": period["Start"], "End": period["EndExclusive"]}

    def test_granularity_follows_the_window_width(self):
        assert live.granularity_for(7) == "DAILY"
        assert live.granularity_for(live.DAILY_MAX_DAYS) == "DAILY"
        assert live.granularity_for(live.DAILY_MAX_DAYS + 1) == "MONTHLY"

    def test_daily_buckets_are_one_per_complete_day(self):
        period = live.window(7)
        buckets = live.period_buckets(period)
        assert len(buckets) == 7
        assert buckets[0][0] == period["Start"]
        assert buckets[-1] == (period["EndInclusive"], period["EndExclusive"])
        assert all(buckets[i][1] == buckets[i + 1][0] for i in range(len(buckets) - 1))

    def test_monthly_buckets_clip_to_the_window(self):
        period = live.window(90)
        assert period["Granularity"] == "MONTHLY"
        buckets = live.period_buckets(period)
        assert buckets[0][0] == period["Start"]
        assert buckets[-1][1] == period["EndExclusive"]
        assert sum(
            (date.fromisoformat(end) - date.fromisoformat(start)).days
            for start, end in buckets) == 90
        # Interior edges land on the first of a month: that is where the API splits.
        assert all(start[8:] == "01" for start, _ in buckets[1:-1])

    def test_only_get_cost_and_usage_is_called(self):
        """Read-only guarantee: the live layer never asks for a mutating API."""
        period = live.window(7)
        client = _fake_ce(grouped={"SERVICE": []}, trend=[])
        live.grouped_costs(client, period, "SERVICE")
        live.trend_points(client, period)
        assert {call[0] for call in client.method_calls} == {"get_cost_and_usage"}

    def test_pagination_merges_every_page(self):
        """Cost Explorer pages large accounts; a partial merge would understate spend."""
        period = live.window(30)
        ec2_page_one = _ce_group("Amazon EC2", "100.00")
        s3_page_two = _ce_group("Amazon S3", "1.00")
        page_one = {
            "ResultsByTime": [_ce_period("2000-01-01", "2000-02-01", groups=[ec2_page_one])],
            "NextPageToken": "token-2",
        }
        page_two = {"ResultsByTime": [_ce_period(
            "2000-02-01", "2000-03-01",
            groups=[_ce_group("Amazon EC2", "50.00"), s3_page_two])]}
        client = MagicMock()
        client.get_cost_and_usage.side_effect = [page_one, page_two]
        records = live.grouped_costs(client, period, "SERVICE")
        assert client.get_cost_and_usage.call_count == 2
        assert client.get_cost_and_usage.call_args_list[1].kwargs["NextPageToken"] == "token-2"
        assert {r["Value"]: r["Cost"] for r in records} == {
            "Amazon EC2": 150.0, "Amazon S3": 1.0}

    def test_negative_amounts_stay_negative(self):
        """Credits are spend the account did not incur; rounding must not drop the sign."""
        period = live.window(7)
        client = _fake_ce(grouped={"SERVICE": [_ce_period(
            "2000-01-01", "2000-02-01",
            groups=[_ce_group("AWS Support", "-20.25"), _ce_group("Amazon EC2", "10.00")])]},
            trend=[_ce_period("2000-01-01", "2000-02-01", total="-20.25")])
        records = live.grouped_costs(client, period, "SERVICE")
        assert [(r["Value"], r["Cost"]) for r in records] == [("Amazon EC2", 10.0),
                                                              ("AWS Support", -20.25)]
        assert live.trend_points(client, period)[0]["Cost"] == -20.25

    def test_near_zero_credit_never_renders_as_negative_zero(self):
        """Real responses carry values like -0.0000003422, which round to -0.0."""
        period = live.window(7)
        client = _fake_ce(grouped={"SERVICE": [_ce_period(
            "2000-01-01", "2000-02-01",
            groups=[_ce_group("Amazon S3", "-0.0000003422")])]},
            trend=[_ce_period("2000-01-01", "2000-02-01", total="-0.0000003422")])
        records = live.grouped_costs(client, period, "SERVICE")
        points = live.trend_points(client, period)
        assert records[0]["Cost"] == 0.0
        assert json.dumps(records[0]["Cost"]) == "0.0"
        assert json.dumps(points[0]["Cost"]) == "0.0"

    def test_a_metric_cost_explorer_omits_is_listed_as_zero(self):
        """An empty Groups metric means no charge recorded, not a missing service."""
        period = live.window(7)
        client = _fake_ce(grouped={"SERVICE": [{"TimePeriod": {"Start": "2000-01-01",
                                                               "End": "2000-02-01"},
                                                "Groups": [{"Keys": ["Amazon S3"]}]}]})
        records = live.grouped_costs(client, period, "SERVICE")
        assert records == [{"PeriodStart": period["Start"],
                            "PeriodEnd": period["EndInclusive"],
                            "Dimension": "SERVICE", "Value": "Amazon S3",
                            "Cost": 0.0, "Currency": "USD"}]

    def test_each_aws_refusal_comes_with_the_right_advice(self):
        """A presenter can only act on an error that names the fix."""
        cases = {
            "AccessDeniedException": "ce:GetCostAndUsage",
            "UninitializedAccount": "Cost Explorer has no data",
            "RequestLimitExceeded": "throttled",
            "NoCredentialsError": "credential",
            "InvalidClientTokenId": "expired session token",
            # The code this account really returns for a stale SSO session.
            "UnrecognizedClientException": "rejected this identity's credentials",
        }
        for code, needle in cases.items():
            exc = ClientError({"Error": {"Code": code, "Message": "api detail"}},
                              "GetCostAndUsage")
            typed = live.error_from(exc)
            assert typed.code == code, code
            assert needle in typed.hint, f"{code} hint: {typed.hint!r}"

    def test_an_unclassified_error_is_still_carried_in_full(self):
        exc = ClientError({"Error": {"Code": "SomeNewAWSCode",
                                     "Message": "line one\nline two"}}, "GetCostAndUsage")
        typed = live.error_from(exc)
        assert typed.code == "SomeNewAWSCode"
        assert typed.message.startswith("ClientError (SomeNewAWSCode): line one")
        assert "line two" not in typed.message
        assert typed.hint == ""

    def test_a_missing_botocore_dependency_is_typed_too(self):
        """
        The failure this machine actually produced when run from the wrong venv.

        botocore raises it before any HTTP request, so there is no Error Code to
        read; the exception class name has to carry the diagnosis.
        """
        exc = MissingDependencyException(msg="CRT is required to make this API call")
        typed = live.error_from(exc)
        assert typed.code == "MissingDependencyException"
        assert "virtualenv" in typed.hint

    def test_client_construction_failure_is_typed(self):
        """boto3 can fail before any request; that failure still needs a hint."""
        with patch("boto3.client", side_effect=NoCredentialsError()):
            with pytest.raises(live.LiveError) as excinfo:
                live.make_client()
        assert excinfo.value.code == "NoCredentialsError"
        assert "Demo" in excinfo.value.hint


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

class TestExports:

    def test_csv_export_columns_and_demo_label(self):
        status, headers, body = app.dispatch(
            "GET", "/api/report/csv", _query("days=30&mode=demo"))
        text = body.decode("utf-8")
        assert status == 200
        assert headers["Content-Type"].startswith("text/csv")
        assert 'attachment; filename="finops-demo-30d.csv"' in headers["Content-Disposition"]
        lines = [line for line in text.splitlines() if line]
        assert lines[0].startswith("PeriodStart,PeriodEnd,Dimension,Value,Cost,Currency,Source")
        assert DEMO_WARNING in text
        # RowType is appended last, mirroring how the CLI derives fieldnames
        # from the first record, so each data row ends with its row type.
        assert any(line.endswith(",service") for line in lines[1:])
        assert any(line.endswith(",region") for line in lines[1:])

    def test_json_export_is_the_whole_payload(self):
        status, headers, body = app.dispatch(
            "GET", "/api/report/json", _query("days=90&mode=demo"))
        payload = json.loads(body.decode("utf-8"))
        assert status == 200
        assert "attachment; filename=" in headers["Content-Disposition"]
        assert payload["meta"]["days"] == 90
        assert payload["services"] and payload["waste"]["findings"]

    def test_export_follows_the_selected_period(self):
        _, _, short = app.dispatch("GET", "/api/report/csv", _query("days=7&mode=demo"))
        _, _, long_ = app.dispatch("GET", "/api/report/csv", _query("days=90&mode=demo"))
        assert float(short.decode("utf-8").splitlines()[1].split(",")[4]) < \
            float(long_.decode("utf-8").splitlines()[1].split(",")[4])

    def test_reports_to_csv_matches_the_cli_fieldnames(self):
        """cost_analysis.export_to_csv derives fieldnames from record[0]; keep parity."""
        _, _, payload = _get_payload("days=30&mode=demo")
        rows = data.csv_rows(payload)
        csv_text = reports.to_csv(rows)
        assert csv_text.splitlines()[0] == ",".join(rows[0].keys())
        assert len(csv_text.splitlines()) == len(rows) + 1

    def test_reports_to_csv_is_empty_for_no_rows(self):
        assert reports.to_csv([]) == ""

    def test_reports_to_json_uses_the_cli_indentation(self):
        assert reports.to_json({"a": 1}) == json.dumps({"a": 1}, indent=2)


# ---------------------------------------------------------------------------
# Data layer guards
# ---------------------------------------------------------------------------

class TestDataGuards:

    def test_build_payload_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            data.build_payload(30, mode="sample")

    def test_build_payload_rejects_unsupported_days(self):
        with pytest.raises(ValueError):
            data.build_payload(45)

    def test_normalize_days_accepts_only_offered_windows(self):
        assert data.normalize_days("7") == 7
        assert data.normalize_days(90) == 90
        for bad in ("15", None, "", "abc", 0, 365):
            assert data.normalize_days(bad) is None, bad

    def test_add_percentages_needs_no_cost_explorer_client(self):
        records = data.add_percentages(data.demo_cost_records(days=30))
        assert abs(sum(r["Percentage"] for r in records) - 100.0) < 0.1
        assert data.add_percentages([]) == []
