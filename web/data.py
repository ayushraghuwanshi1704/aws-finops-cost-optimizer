"""
Dashboard data provider.

One payload shape serves both modes:

  * demo - built from web/demo_data.py and cost_analysis.demo_cost_records().
    Makes no AWS call of any kind, so it works offline and without credentials.
  * live - billing spend comes from web/live.py (read-only Cost Explorer
    queries), waste and right-sizing from the existing analysis classes in
    scripts/. Each section reports why a number is missing instead of
    inventing one.

No sample figure is ever mixed into a live payload and no live figure is mixed
into a sample payload: the mode is decided once and carried in `meta`.
"""
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from cost_analysis import (
    DEMO_BASELINE_DAYS,
    DEMO_WARNING,
    CostAnalyzer,
    demo_cost_records,
)
from commitment_coverage import CommitmentAnalyzer
from rightsizing_recommendations import RightSizingRecommender
from unused_resources import UnusedResourceFinder, build_summary

from web import demo_data, ebs, live

MODES = ("demo", "live")
SUPPORTED_DAYS = (7, 30, 90)

# Sections a live run must fetch from AWS. Demo mode answers all of them locally.
SECTIONS = ("services", "regions", "trend", "waste", "rightsizing", "commitments")

# Finding buckets unused_resources.py writes, so a failed live scan still
# produces the same keys the UI iterates over.
WASTE_KEYS = (
    "IdleEC2Instances",
    "UnattachedEBSVolumes",
    "UnusedElasticIPs",
    "OldEBSSnapshots",
    "IdleRDSInstances",
)


LIVE_STATUSES = ("connected", "connected-zero", "connected-empty", "partial", "unavailable")

# One cent is the smallest amount the dashboard can show as a number that is not
# $0.00, so it is also the line between "spend to chart" and "nothing material".
MATERIAL_SPEND_USD = 0.01


def status_for(mode: str, errors: List[Dict], services: List[Dict],
               total_cost: float) -> Dict[str, str]:
    """
    Describe what the live connection actually did.

    A live window where every service line rounds to $0.00 is a correct answer
    from Cost Explorer, but the page cannot tell that apart from a query that
    never ran - so the distinction is worked out here, in the layer the tests
    cover, and carried to the UI in `meta.status`. The wording never claims a
    failure that did not happen, and never claims data that was not returned.
    """
    if mode == "demo":
        return {
            "State": "demo",
            "Message": "Demo figures - no AWS API was called for these numbers.",
        }

    if errors:
        if services:
            return {
                "State": "partial",
                "Message": (
                    f"Live AWS data partially connected - billing figures below are real, "
                    f"but {len(errors)} section(s) could not be read."
                ),
            }
        return {
            "State": "unavailable",
            "Message": (
                "Live AWS data unavailable - Cost Explorer could not be read. "
                "The figures below are not live data."
            ),
        }

    if not services:
        return {
            "State": "connected-empty",
            "Message": (
                "Live AWS data connected - Cost Explorer returned no cost rows "
                "for this period, so the account recorded no billable usage."
            ),
        }

    if abs(total_cost) < MATERIAL_SPEND_USD:
        # A currency symbol the account does not bill in would be its own lie,
        # so a non-USD window is described by the code AWS returned instead.
        currency = services[0].get("Currency", "USD") if services else "USD"
        zero = "$0.00" if currency == "USD" else f"0.00 {currency}"
        return {
            "State": "connected-zero",
            "Message": (
                f"Live AWS data connected - no material spend in this period. "
                f"{len(services)} service line(s) returned, each rounding to {zero}."
            ),
        }

    return {
        "State": "connected",
        "Message": "Live AWS data connected - Cost Explorer returned spend for this period.",
    }


def normalize_days(value: Any) -> Optional[int]:
    """Return `value` as one of SUPPORTED_DAYS, or None if it is not a filter we offer."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return days if days in SUPPORTED_DAYS else None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Reaches calculate_percentage() without ever building a Cost Explorer client,
# so demo and live payloads share one percentage implementation.
_PERCENTAGE_ENGINE = CostAnalyzer(demo=True)


def add_percentages(records: List[Dict]) -> List[Dict]:
    """
    Attach `Percentage` using the same helper the CLI uses.

    _PERCENTAGE_ENGINE exists so the shared maths runs without a Cost Explorer
    client; demo and live payloads get percentages from one implementation.
    """
    if not records:
        return records
    return _PERCENTAGE_ENGINE.calculate_percentage(list(records))


def totals_from(
    services: List[Dict],
    waste_summary: Dict,
    days: int,
) -> Dict[str, Any]:
    """Metric-card figures derived from the records above them, never stored separately."""
    spend = round(sum(record["Cost"] for record in services), 2)
    top = services[0] if services else None
    return {
        "PeriodDays": days,
        "TotalCostUSD": spend,
        "Currency": services[0]["Currency"] if services else "USD",
        "MonthlyRunRateUSD": round(spend * (DEMO_BASELINE_DAYS / days), 2),
        "TopService": top["Value"] if top else None,
        "TopServicePercent": top.get("Percentage") if top else None,
        "WasteFindings": waste_summary.get("TotalFindings", 0),
        "EstimatedMonthlySavingsUSD": waste_summary.get("EstimatedMonthlySavingsUSD", 0.0),
        "UnquantifiedFindings": waste_summary.get("UnquantifiedFindings", 0),
    }


def _safe_call(section: str, errors: List[Dict], fetch: Callable[[], Any],
               hint: str = "") -> Any:
    """
    Run one live collector, turning its failure into a readable message.

    The scripts in scripts/ call sys.exit() when Cost Explorer rejects a query.
    Inside a request handler that must not end the process, so SystemExit is
    caught here and reported like any other failure. A web.live.LiveError keeps
    its AWS error code, which is what lets the UI say "access denied" rather
    than "something went wrong".
    """
    try:
        return fetch()
    except SystemExit as exc:
        entry = {
            "Section": section,
            "Code": "SystemExit",
            "Message": (
                "AWS rejected this query (exit code "
                f"{exc.code}). Cost Explorer may be disabled for this account."
            ),
        }
        hint = hint or (
            "Enable Cost Explorer for the account, or present in Demo mode."
        )
    except Exception as exc:  # botocore raises many types; none should break the page
        entry = {
            "Section": section,
            "Code": str(getattr(exc, "code", type(exc).__name__)),
            "Message": str(getattr(exc, "message", "") or
                           f"{type(exc).__name__}: {_redact(str(exc))}"),
        }
        hint = hint or str(getattr(exc, "hint", ""))
    if hint:
        entry["Hint"] = hint
    errors.append(entry)
    return None


def _redact(message: str) -> str:
    """Trim botocore messages to something a presenter can read on screen."""
    first_line = message.strip().splitlines()[0] if message.strip() else "unknown error"
    return first_line[:240]


def relabel(records: List[Dict], period: Dict[str, Any]) -> List[Dict]:
    """
    Point every cost row at the window actually queried.

    Demo records are built by cost_analysis.demo_cost_records(), whose labels
    come from the CLI's own window rule; the dashboard shows one date range for
    both modes, so the labels are aligned here. Amounts are never touched.
    """
    for record in records:
        record["PeriodStart"] = period["Start"]
        record["PeriodEnd"] = period["EndInclusive"]
    return records


def demo_payload(days: int) -> Dict[str, Any]:
    """The full dashboard payload from sample data only. Never touches AWS."""
    # live.window() is pure date arithmetic - calling it makes no AWS request.
    period = live.window(days)
    services = add_percentages(relabel(demo_cost_records(days=days), period))
    regions = add_percentages(relabel(demo_data.demo_region_records(days), period))
    waste = demo_data.demo_waste_findings(days)
    waste_summary = build_summary(waste)
    totals = totals_from(services, waste_summary, days)

    return {
        "meta": {
            "mode": "demo",
            "status": status_for("demo", [], services, totals["TotalCostUSD"]),
            "warning": DEMO_WARNING,
            "days": days,
            "periodStart": period["Start"],
            "periodEnd": period["EndInclusive"],
            "period": period,
            "granularity": period["Granularity"],
            "regionScope": "sample account (all regions)",
            "generatedAt": now_iso(),
            "dataSource": (
                "Sample figures in cost_analysis.DEMO_SAMPLE_MONTHLY_COSTS and "
                "web/demo_data.py. No AWS API was called."
            ),
            "aws": aws_status(),
        },
        "totals": totals,
        "services": services,
        "regions": regions,
        "trend": demo_data.demo_trend(days, period),
        "waste": {"summary": waste_summary, "findings": waste},
        "rightsizing": demo_data.demo_rightsizing(days),
        "commitments": {
            "AnalysisPeriodDays": days,
            "SavingsPlans": dict(demo_data.DEMO_SAVINGS_PLANS),
            "ReservedInstances": dict(demo_data.DEMO_RESERVED_INSTANCES),
            "Observations": [
                "Savings Plan utilization is 62.4%: part of the purchased "
                "commitment is unused, so it is worth reviewing the terms.",
                "Coverage is 41.8% while spend is steady: the uncovered balance "
                "still runs at on-demand rates.",
                "Purchase decisions need workload context and belong to a human.",
            ],
        },
        "errors": [],
    }


def live_payload(days: int, region: str) -> Dict[str, Any]:
    """The dashboard payload from real AWS data, with per-section honest failures."""
    period = live.window(days)
    errors: List[Dict] = []

    # Creating a client can itself fail (unresolvable credentials or profile),
    # and that must surface as a message rather than a broken page.
    client = _safe_call(
        "cost explorer client", errors, live.make_client,
        hint=(
            "Live mode needs a usable AWS credential profile. Switch the data "
            "source back to Demo to continue presenting; no sample figure is "
            "shown as live data."
        ),
    )

    services = regions = trend = None
    if client is not None:
        services = _safe_call(
            "services", errors,
            lambda: add_percentages(live.grouped_costs(client, period, "SERVICE")))
        regions = _safe_call(
            "regions", errors,
            lambda: add_percentages(live.grouped_costs(client, period, "REGION")))
        trend = _safe_call("trend", errors, lambda: live.trend_points(client, period))

    waste = _safe_call(
        "waste", errors,
        lambda: UnusedResourceFinder(region=region).scan_all(ec2_days=max(days, 7)),
        hint=(
            f"The EC2/RDS/CloudWatch scan in {region} could not run, so the waste "
            "table is empty rather than clean. Fix the profile, or present in Demo "
            "mode."))
    rightsizing = _safe_call(
        "rightsizing", errors,
        lambda: RightSizingRecommender(region=region).get_recommendations(days=days),
        hint=(
            f"CloudWatch utilisation in {region} could not be read, so no "
            "right-sizing recommendation is shown - not that there are none."))
    commitments = _safe_call(
        "commitments", errors,
        lambda: CommitmentAnalyzer(region="us-east-1").build_report(days),
        hint=(
            "Savings Plans and RI utilization/coverage could not be read, so the "
            "commitment cards show 'none' - which means unknown here, not unpaid."))

    services = services or []
    regions = regions or []
    waste = waste or {name: [] for name in WASTE_KEYS}
    waste_summary = build_summary(waste)
    totals = totals_from(services, waste_summary, days)
    status = status_for("live", errors, services, totals["TotalCostUSD"])

    return {
        "meta": {
            "mode": "live",
            "status": status,
            "warning": None,
            "days": days,
            "periodStart": period["Start"],
            "periodEnd": period["EndInclusive"],
            "period": period,
            "granularity": period["Granularity"],
            "regionScope": region,
            "generatedAt": now_iso(),
            "dataSource": (
                f"Cost Explorer GetCostAndUsage (us-east-1, {period['Granularity']}, "
                f"UnblendedCost) for spend; EC2/RDS/CloudWatch in {region} for "
                "waste and right-sizing."
            ),
            "aws": aws_status(),
        },
        "totals": totals_from(services, waste_summary, days),
        "services": services,
        "regions": regions,
        "trend": trend or [],
        "waste": {"summary": waste_summary, "findings": waste},
        "rightsizing": rightsizing or [],
        "commitments": commitments or {
            "AnalysisPeriodDays": days,
            "SavingsPlans": {"Utilization": None, "Coverage": None},
            "ReservedInstances": {"Utilization": None, "Coverage": None},
            "Observations": [],
        },
        "errors": errors,
    }


def build_payload(days: int, mode: str = "demo", region: str = "us-east-1") -> Dict[str, Any]:
    """Entry point for the API: one payload, one mode, never both."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    if days not in SUPPORTED_DAYS:
        raise ValueError(f"unsupported day filter: {days!r}")
    if mode == "demo":
        return demo_payload(days)
    return live_payload(days, region)


def ebs_payload(mode: str, regions: List[str]) -> Dict[str, Any]:
    """
    EBS volume status on its own, because it refreshes far more often than billing.

    A separate collector keeps the table's polling cost at one `DescribeVolumes`
    per region instead of a full Cost Explorer run, and it keeps the two modes
    apart the same way `build_payload()` does: demo answers from labelled
    samples and makes no AWS call, live answers from AWS and reports each failed
    region as an error. No sample volume is ever placed in a live response.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    if not regions:
        raise ValueError("at least one region is required")

    if mode == "demo":
        volumes = demo_data.demo_volumes(regions)
        errors: List[Dict] = []
        queried: List[str] = []
        source = (
            "Sample volume rows in web/demo_data.py. No AWS API was called and "
            "no EC2 permission is needed."
        )
    else:
        collected = ebs.collect(regions)
        volumes = collected["Volumes"]
        errors = collected["Errors"]
        queried = collected["RegionsQueried"]
        source = (
            "EC2 DescribeVolumes (read-only) in "
            f"{', '.join(queried) if queried else 'no region'}; "
            "attachment state comes from AWS, not from inference."
        )

    return {
        "meta": {
            "mode": mode,
            "status": ebs.status_for(mode, errors, volumes),
            "regionsRequested": list(regions),
            "regionsQueried": queried,
            "fetchedAt": now_iso(),
            "dataSource": source,
            "readOnlyAction": "ec2:DescribeVolumes",
            "aws": aws_status(),
        },
        "volumes": volumes,
        "errors": errors,
    }


def aws_status() -> Dict[str, Any]:
    """
    Describe AWS reachability without making an AWS API call.

    Credential resolution is a local config lookup, so it is safe to attempt;
    the result is a status word and a short reason only - never a key, token,
    account id or profile path. Cost Explorer enablement cannot be known
    without calling the API, so it is reported as unverified rather than guessed.
    """
    status: Dict[str, Any] = {
        "credentials": "unknown",
        "detail": "",
        "costExplorerEnabled": "unverified (no AWS call is made by the dashboard health probe)",
        "apiCallsMade": False,
    }
    try:
        import boto3

        credentials = boto3.session.Session().get_credentials()
    except Exception as exc:  # credential provider failures vary widely
        status["credentials"] = "error"
        status["detail"] = f"{type(exc).__name__}: {_redact(str(exc))}"
        return status

    if credentials is None:
        status["credentials"] = "unavailable"
        status["detail"] = (
            "No credentials resolved, so live mode cannot return real billing data. "
            "Demo mode is unaffected."
        )
        return status

    status["credentials"] = "available"
    status["detail"] = "Credentials resolved locally; no AWS API call has been made."
    return status


def csv_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cost rows for the CSV download: services first, then regions, both labelled."""
    rows: List[Dict[str, Any]] = []
    for kind, records in (("service", payload["services"]), ("region", payload["regions"])):
        for record in records:
            row = dict(record)
            row["RowType"] = kind
            rows.append(row)
    return rows
