"""
Live Cost Explorer access for the dashboard.

This module exists rather than calling `CostAnalyzer.analyze_costs()` for three
reasons that only matter to an HTTP server:

  * the CLI scripts `sys.exit(1)` when a query fails, which is correct for a
    terminal but throws away the API error code the dashboard needs to tell
    "access denied" apart from "Cost Explorer not enabled";
  * the CLI fixes MONTHLY granularity, and a 7-day window then comes back as a
    single bucket, which is not a chart;
  * `TimePeriod.End` is exclusive, so the payload must state the range the data
    actually covers rather than the range that was asked for.

The records returned here are field-for-field the shape `analyze_costs()`
produces, so percentage calculation, totals, CSV/JSON export and the UI code
path stay shared between demo and live.
"""
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

METRIC = "UnblendedCost"

# Cost Explorer is a global API served from us-east-1, like the CLI scripts assume.
CE_REGION = "us-east-1"

# A window this long or shorter is charted day by day; longer ones by calendar
# month. 90 daily bars are unreadable in a panel, and MONTHLY is the only other
# granularity Cost Explorer offers for a period this wide.
DAILY_MAX_DAYS = 31

# Error codes grouped by what the presenter can actually do about them.
ERROR_HINTS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (
        ("AccessDenied", "AccessDeniedException", "Forbidden", "UnauthorizedOperation",
         "Client.UnauthorizedOperation"),
        "This identity is not allowed to call ce:GetCostAndUsage. Attach a read-only "
        "Cost Explorer policy to the profile the server uses, or present in Demo mode.",
    ),
    (
        ("UninitializedAccount", "DataUnavailable", "BillExpirationException",
         "BillingInProgress"),
        "Cost Explorer has no data for this account yet. It needs up to 24 hours of "
        "backfill after first activation, and the current month is still closing.",
    ),
    (
        ("InvalidClientTokenId", "InvalidAccessKeyId", "SignatureDoesNotMatch",
         "IncompleteSignature", "AuthFailure", "ExpiredToken", "TokenRefreshRequired",
         "UnidentifiedCredentialsException", "UnrecognizedClientException",
         # Raised by the `login` credential provider on this machine when the SSO
         # session behind the profile has run out; observed from a live request.
         "LoginRefreshRequired"),
        "AWS rejected this identity's credentials - an expired session token or the "
        "wrong keys. Refresh the login/SSO session for the profile the server uses, "
        "or present in Demo mode.",
    ),
    (
        ("RequestLimitExceeded", "Throttling", "TooManyRequestsException"),
        "Cost Explorer throttled this request. Wait a moment and press Refresh.",
    ),
    (
        ("NoCredentialsError", "CredentialProviderError", "ProfileNotFound",
         "MissingDependencyError", "MissingDependencyException", "NoRegionError",
         "PartialCredentials"),
        "This process could not build a Cost Explorer client - no usable AWS "
        "credential profile, or a required botocore dependency is missing. Run "
        "the server from the repository virtualenv, or choose Demo to keep "
        "presenting; live numbers cannot be invented.",
    ),
)


class LiveError(Exception):
    """A live query failure the dashboard can explain, with its AWS error code."""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _first_line(text: str) -> str:
    stripped = text.strip()
    return (stripped.splitlines()[0] if stripped else "no further detail")[:240]


def _hint_for(code: str) -> str:
    for codes, hint in ERROR_HINTS:
        if any(token.lower() in code.lower() for token in codes):
            return hint
    return ""


def hint_for(code: str) -> str:
    """
    The advice this module holds for one AWS error code, or "".

    Public because `web/ebs.py` shares every code grouping except the
    access-denied advice, which has to name `ec2:DescribeVolumes` instead of
    `ce:GetCostAndUsage`.
    """
    return _hint_for(code)


def error_from(exc: Exception) -> LiveError:
    """Turn a botocore failure into a LiveError carrying the API's own code."""
    response = getattr(exc, "response", None) or {}
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = error.get("Code") or type(exc).__name__
    detail = _first_line(error.get("Message", "") or str(exc))
    return LiveError(code, f"{type(exc).__name__} ({code}): {detail}", hint=_hint_for(code))


def granularity_for(days: int) -> str:
    return "DAILY" if days <= DAILY_MAX_DAYS else "MONTHLY"


def period_buckets(period: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    The (start, end) pairs Cost Explorer will answer with for this window.

    Verified against the live API: DAILY returns one row per complete day and
    MONTHLY returns one row per calendar month touched, first and last of which
    are partial. Demo mode uses the same buckets so a sample chart has the same
    shape as a real one for the same filter.
    """
    first = date.fromisoformat(period["Start"])
    # EndExclusive is where Cost Explorer stops, so the last covered day is one
    # before it.
    last = date.fromisoformat(period["EndExclusive"]) - timedelta(days=1)

    if period["Granularity"] == "DAILY":
        return [(
            (first + timedelta(days=offset)).isoformat(),
            (first + timedelta(days=offset + 1)).isoformat(),
        ) for offset in range((last - first).days + 1)]

    buckets: List[Tuple[str, str]] = []
    cursor = first
    while cursor <= last:
        month_end = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        bucket_end = min(month_end, last + timedelta(days=1))
        buckets.append((cursor.isoformat(), bucket_end.isoformat()))
        cursor = month_end
    return buckets


def window(days: int) -> Dict[str, Any]:
    """
    The period a `days` filter actually queries.

    `EndExclusive` is what is sent to Cost Explorer; `EndInclusive` is the last
    day the response can contain, which is what the dashboard labels the window
    with. Today is therefore never reported as billed - its data is incomplete
    for hours, and Cost Explorer would exclude it anyway.
    """
    today = date.today()
    start = today - timedelta(days=days)
    return {
        "Days": days,
        "Start": start.isoformat(),
        "EndExclusive": today.isoformat(),
        "EndInclusive": (today - timedelta(days=1)).isoformat(),
        "Granularity": granularity_for(days),
        "Note": (
            "Cost Explorer's End date is exclusive: this window covers complete "
            f"days up to and including {today - timedelta(days=1)}."
        ),
    }


def make_client() -> Any:
    """
    Build the Cost Explorer client, or explain why it could not be built.

    Credential resolution happens at client construction, so an unusable profile
    surfaces here rather than on the first query.
    """
    try:
        import boto3

        return boto3.client("ce", region_name=CE_REGION)
    except Exception as exc:  # botocore raises a family of these
        raise error_from(exc)


def query(client: Any, period: Dict[str, str], granularity: str,
          group_by: Optional[str] = None) -> List[Dict]:
    """One Cost Explorer call chain, all pages merged, failures typed."""
    params: Dict[str, Any] = {
        "TimePeriod": {"Start": period["Start"], "End": period["EndExclusive"]},
        "Granularity": granularity,
        "Metrics": [METRIC],
    }
    if group_by:
        params["GroupBy"] = [{"Type": "DIMENSION", "Key": group_by}]

    results: List[Dict] = []
    try:
        while True:
            response = client.get_cost_and_usage(**params)
            results.extend(response.get("ResultsByTime", []))
            token = response.get("NextPageToken")
            if not token:
                break
            params["NextPageToken"] = token
    except (ClientError, BotoCoreError) as exc:
        raise error_from(exc)
    return results


def _amount(container: Dict) -> Tuple[float, str]:
    """Read one UnblendedCost metric, keeping its sign so credits survive."""
    metric = (container.get("Metrics") or {}).get(METRIC) or container.get(METRIC) or {}
    try:
        amount = float(metric.get("Amount", 0.0) or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    return amount, str(metric.get("Unit") or "USD")


def _round2(value: float) -> float:
    """
    Round to cents without producing a signed zero.

    Cost Explorer returns sub-cent credits such as -0.0000003, which round to
    -0.0 and would serialise into the payload as "-0.0". Adding a positive zero
    keeps the value and drops the sign.
    """
    return round(value, 2) + 0.0


def grouped_costs(client: Any, period: Dict[str, Any], dimension: str) -> List[Dict]:
    """
    Window totals per service or region, one record per dimension value.

    Cost Explorer returns a record per period per group, so a 90-day MONTHLY
    query yields the same service three times; summing them is what makes the
    service table agree with the window total.
    """
    results = query(client, period, period["Granularity"], dimension)
    totals: Dict[str, Dict[str, Any]] = {}

    for result in results:
        for group in result.get("Groups", []):
            keys = group.get("Keys") or [""]
            name = str(keys[0]).strip() or "Uncategorized"
            amount, unit = _amount(group)
            record = totals.get(name)
            if record is None:
                record = {
                    "PeriodStart": period["Start"],
                    "PeriodEnd": period["EndInclusive"],
                    "Dimension": dimension,
                    "Value": name,
                    "Cost": 0.0,
                    "Currency": unit,
                }
                totals[name] = record
            record["Cost"] += amount

    records = [{**record, "Cost": _round2(record["Cost"])} for record in totals.values()]
    records.sort(key=lambda record: record["Cost"], reverse=True)
    return records


def label_for(period_start: str, granularity: str) -> str:
    """A short axis label: 'MM-DD' for days, 'YYYY-MM' for months."""
    if granularity == "DAILY":
        return period_start[5:]
    return period_start[:7]


def trend_points(client: Any, period: Dict[str, Any]) -> List[Dict]:
    """
    Ungrouped totals for the window, one point per Cost Explorer period.

    `PeriodEnd` is kept as the API returned it (exclusive) and `Estimated` is
    passed through, because Cost Explorer marks recent periods as estimates and
    a chart should not imply more precision than the billing data has.
    """
    granularity = period["Granularity"]
    points: List[Dict] = []
    for result in query(client, period, granularity):
        time_period = result.get("TimePeriod") or {}
        start = str(time_period.get("Start", ""))
        amount, unit = _amount(result.get("Total") or {})
        points.append({
            "Period": start,
            "Label": label_for(start, granularity) if start else "",
            "Cost": _round2(amount),
            "Currency": unit,
            "PeriodStart": start,
            "PeriodEnd": str(time_period.get("End", "")),
            "Granularity": granularity,
            "Estimated": bool(result.get("Estimated", False)),
        })
    return points
