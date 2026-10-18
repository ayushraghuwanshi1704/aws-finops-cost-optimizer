"""
Sample data for the dashboard's DEMO mode.

Nothing here is read from AWS and nothing here reaches AWS: demo requests are
served entirely from these constants so the UI can be shown offline, without
credentials, without Cost Explorer being enabled, and without a network.

Every record carries the shared `DEMO_WARNING` label imported from
cost_analysis.py, so sample data can never be mistaken for live billing.

The service-level figures are owned by cost_analysis.DEMO_SAMPLE_MONTHLY_COSTS
(a 30-day baseline) and are reused here for the totals, the regional split and
the trend so the dashboard always agrees with `cost_analysis.py --demo`.
"""
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from cost_analysis import DEMO_BASELINE_DAYS, DEMO_SAMPLE_MONTHLY_COSTS, DEMO_WARNING
from web import live
# Share of the monthly sample bill attributed to each region. Chosen to sum to
# 1.0 so the region table reconciles with the service table to the cent.
DEMO_REGION_SHARES: Dict[str, float] = {
    "us-east-1": 0.46,
    "eu-west-1": 0.27,
    "ap-south-1": 0.17,
    "us-west-2": 0.10,
}

# A sample account bills less at weekends: batch jobs are paused, so daily
# spend dips. Applied per day, which is what makes a DAILY demo chart read as
# a week rhythm rather than as noise.
DEMO_WEEKEND_FACTOR = 0.55

# Oldest-to-newest multiplier for MONTHLY buckets, so a 90-day sample chart
# rises the way the account it represents is meant to.
DEMO_TREND_RAMP: Tuple[float, float] = (0.85, 1.0)

# Illustrative commitment posture for a mid-size account.
#
# Key-for-key the same shape CommitmentAnalyzer.build_report() returns: each of
# Utilization and Coverage is a dict of rounded numbers, never a bare float.
# The UI reads the inner percentage key, so a flatter demo shape would render
# fine in demo mode and show "n/a" against a real account.
DEMO_SAVINGS_PLANS: Dict[str, Optional[Dict[str, float]]] = {
    "Utilization": {
        "UtilizationPercentage": 62.4,
        "UnusedCommitmentUSD": 180.00,
        "NetSavingsUSD": 412.00,
    },
    "Coverage": {
        "CoveragePercentage": 41.8,
        "UncoveredOnDemandCostUSD": 4890.00,
    },
}
DEMO_RESERVED_INSTANCES: Dict[str, Optional[Dict[str, float]]] = {
    "Utilization": {
        "UtilizationPercentage": 88.1,
        "UnusedHours": 58.2,
        "NetRISavingsUSD": 240.00,
    },
    # 369 reserved of 1102 total hours = 33.48%, rounded the way the analyzer rounds.
    "Coverage": {
        "CoverageHoursPercentage": 33.5,
        "OnDemandHours": 733.0,
        "ReservedHours": 369.0,
    },
}

DEMO_UNQUANTIFIED_NOTE = (
    "Idle EC2/RDS compute savings are not quantified here - see each "
    "finding's recommendation."
)


def monthly_sample_total() -> float:
    """The 30-day baseline total behind every demo figure."""
    return round(sum(DEMO_SAMPLE_MONTHLY_COSTS.values()), 2)


def scale_window(days: int, baseline_days: int) -> float:
    """Factor that turns a monthly baseline figure into a `days`-long window."""
    return days / baseline_days


def demo_region_records(days: int) -> List[Dict]:
    """
    Regional split of the same sample spend used for the service table.

    The last region absorbs the rounding remainder so the region costs always
    sum to the service total instead of drifting by a cent.
    """
    total = round(monthly_sample_total() * scale_window(days, DEMO_BASELINE_DAYS), 2)
    scale = scale_window(days, DEMO_BASELINE_DAYS)
    records: List[Dict] = []
    allocated = 0.0
    regions = list(DEMO_REGION_SHARES.items())

    for index, (region, share) in enumerate(regions):
        if index == len(regions) - 1:
            cost = round(total - allocated, 2)
        else:
            cost = round(monthly_sample_total() * share * scale, 2)
            allocated += cost
        records.append({
            "Value": region,
            "Cost": cost,
            "Currency": "USD",
            "Source": DEMO_WARNING,
        })

    records.sort(key=lambda record: record["Cost"], reverse=True)
    return records


def _trend_weights(buckets: List[Tuple[str, str]], granularity: str) -> List[float]:
    """
    Relative sample spend per bucket, oldest first, normalised to sum to 1.

    Deterministic on purpose: a demo chart must look the same on every refresh,
    and weekday/weekend and month-count shapes come from the real period
    boundaries rather than from a fixed six-point script.
    """
    raw: List[float] = []
    count = len(buckets)

    for index, (start, end) in enumerate(buckets):
        first = date.fromisoformat(start)
        span = (date.fromisoformat(end) - first).days
        if granularity == "DAILY":
            shape = DEMO_WEEKEND_FACTOR if first.weekday() >= 5 else 1.0
        else:
            low, high = DEMO_TREND_RAMP
            shape = high if count == 1 else low + (high - low) * index / (count - 1)
        raw.append(span * shape)

    total = sum(raw) or 1.0
    return [weight / total for weight in raw]


def demo_trend(days: int, period: Dict[str, Any]) -> List[Dict]:
    """
    Sample spend laid over the periods Cost Explorer would return for `period`.

    Demo and live charts share one schema because both walk
    `live.period_buckets()` for the same window, so a 7-day filter shows seven
    daily bars and a 90-day filter shows monthly bars in either mode. The last
    bucket absorbs the rounding remainder, which is why the bars always add up
    to the window total on the metric cards.
    """
    buckets = live.period_buckets(period)
    if not buckets:
        return []

    window_total = round(monthly_sample_total() * scale_window(days, DEMO_BASELINE_DAYS), 2)
    weights = _trend_weights(buckets, period["Granularity"])
    trend: List[Dict] = []
    allocated = 0.0

    for index, ((start, end), weight) in enumerate(zip(buckets, weights)):
        if index == len(buckets) - 1:
            amount = round(window_total - allocated, 2)
        else:
            amount = round(window_total * weight, 2)
            allocated += amount
        trend.append({
            "Period": start,
            "Label": live.label_for(start, period["Granularity"]),
            "Cost": amount,
            "Currency": "USD",
            "PeriodStart": start,
            "PeriodEnd": end,
            "Granularity": period["Granularity"],
            "Estimated": False,
            "Source": DEMO_WARNING,
        })

    return trend


def demo_waste_findings(days: int) -> Dict[str, List[Dict]]:
    """
    Waste findings in the exact shape produced by unused_resources.py.

    Field names match that script's output so the dashboard renders demo and
    live findings with one code path.
    """
    label = {"Source": DEMO_WARNING, "Region": "us-east-1", "AnalysisDays": days}

    return {
        "IdleEC2Instances": [
            {
                "InstanceId": "i-0demo1a2b3c4d5e6f",
                "InstanceType": "m5.2xlarge",
                "Finding": "Idle EC2 Instance",
                "Metric": f"Max of daily-average CPU over {days}d",
                "Value": "3.10%",
                "EstimatedMonthlySavingsUSD": None,
                "Recommendation": (
                    "Review with the owner: stop, downsize, or put on a "
                    "start/stop schedule. Savings equal the instance's "
                    "compute price - quantify via cost_analysis.py "
                    "grouped by INSTANCE_TYPE."
                ),
                **label,
            },
            {
                "InstanceId": "i-0demo9z8y7x6w5v4u3",
                "InstanceType": "c5.xlarge",
                "Finding": "Idle EC2 Instance",
                "Metric": f"Max of daily-average CPU over {days}d",
                "Value": "4.60%",
                "EstimatedMonthlySavingsUSD": None,
                "Recommendation": (
                    "Review with the owner: stop, downsize, or put on a "
                    "start/stop schedule."
                ),
                **label,
            },
        ],
        "UnattachedEBSVolumes": [
            {
                "VolumeId": "vol-0demovolume0001",
                "VolumeType": "gp3",
                "SizeGiB": 250,
                "Finding": "Unattached EBS Volume",
                "EstimatedMonthlySavingsUSD": 28.75,
                "Recommendation": "Snapshot then delete the volume, or reattach it",
                **label,
            },
            {
                "VolumeId": "vol-0demovolume0002",
                "VolumeType": "gp2",
                "SizeGiB": 500,
                "Finding": "Unattached EBS Volume",
                "EstimatedMonthlySavingsUSD": 60.00,
                "Recommendation": "Snapshot then delete the volume, or reattach it",
                **label,
            },
        ],
        "UnusedElasticIPs": [
            {
                "AllocationId": "eipalloc-0demoeip00001",
                "PublicIp": "203.0.113.10",
                "Finding": "Unused Elastic IP",
                "EstimatedMonthlySavingsUSD": 3.65,
                "Recommendation": "Release the address if nothing will reuse it",
                **label,
            },
        ],
        "OldEBSSnapshots": [
            {
                "SnapshotId": "snap-0demosnapshot001",
                "Description": "weekly-backup (sample)",
                "StartTimeDaysAgo": 148,
                "VolumeSizeGiB": 120,
                "Finding": "Old EBS Snapshot",
                "EstimatedMonthlySavingsUSD": 24.00,
                "Recommendation": "Delete once no AMI or restore point needs it",
                **label,
            },
        ],
        "IdleRDSInstances": [
            {
                "DBInstanceIdentifier": "demo-analytics-idle",
                "DBInstanceClass": "db.r5.xlarge",
                "Engine": "postgres",
                "Finding": "Idle RDS Instance",
                "Metric": f"Max of daily-average DatabaseConnections over {days}d",
                "Value": "0",
                "EstimatedMonthlySavingsUSD": None,
                "Recommendation": (
                    "Confirm no scheduled jobs use it, then stop before deleting."
                ),
                **label,
            },
        ],
    }


def demo_rightsizing(days: int) -> List[Dict]:
    """Right-sizing recommendations in the shape produced by rightsizing_recommendations.py."""
    return [
        {
            "InstanceId": "i-0demo1a2b3c4d5e6f",
            "InstanceName": "sample-analytics-worker",
            "InstanceType": "m5.2xlarge",
            "Region": "us-east-1",
            "AnalysisPeriodDays": days,
            "Finding": "Potentially over-provisioned",
            "MaxCPUPercent": 21.40,
            "MaxMemoryPercent": 34.80,
            "MemoryDataSource": "cw_agent",
            "Recommendation": (
                "Consider a smaller instance in the same family (current: "
                "m5.2xlarge). Validate with application owner before resizing."
            ),
            "Source": DEMO_WARNING,
        },
        {
            "InstanceId": "i-0demo9z8y7x6w5v4u3",
            "InstanceName": "sample-batch-tasks",
            "InstanceType": "c5.xlarge",
            "Region": "us-east-1",
            "AnalysisPeriodDays": days,
            "Finding": "Potentially over-provisioned",
            "MaxCPUPercent": 18.20,
            "MaxMemoryPercent": "N/A",
            "MemoryDataSource": "no_cw_agent",
            "MemoryNote": "CloudWatch Agent not detected; memory utilisation unknown.",
            "Recommendation": (
                "Consider a smaller instance in the same family (current: "
                "c5.xlarge). Validate with application owner before resizing."
            ),
            "Source": DEMO_WARNING,
        },
    ]


def demo_volumes(regions: List[str]) -> List[Dict]:
    """
    Volumes in the exact shape web/ebs.py produces.

    Field names and derived labels match the live records one-for-one, so the
    table renders sample and real volumes through the same code path. Every row
    carries DEMO_WARNING: a demo volume must never be able to read as an account
    finding, and one row per AWS state is here to show what each label means.
    """
    region = regions[0] if regions else "us-east-1"
    secondary = regions[1] if len(regions) > 1 else region
    label = {"Source": DEMO_WARNING}

    return [
        {
            "VolumeId": "vol-0demo1a2b3c4d5e6f",
            "VolumeState": "in-use",
            "AttachmentState": "Attached",
            "InstanceId": "i-0demo1a2b3c4d5e6f",
            "AttachmentCount": 1,
            "AvailabilityZone": f"{region}a",
            "SizeGiB": 120,
            "Region": region,
            **label,
        },
        {
            # available with no attachment is AWS's own definition of detached,
            # and it is the row the waste table also charges for.
            "VolumeId": "vol-0demo9z8y7x6w5v4u3",
            "VolumeState": "available",
            "AttachmentState": "Detached",
            "InstanceId": None,
            "AttachmentCount": 0,
            "AvailabilityZone": f"{region}b",
            "SizeGiB": 30,
            "Region": region,
            **label,
        },
        {
            "VolumeId": "vol-0democreating0001",
            "VolumeState": "creating",
            "AttachmentState": "Not attached: Creating",
            "InstanceId": None,
            "AttachmentCount": 0,
            "AvailabilityZone": f"{region}c",
            "SizeGiB": 8,
            "Region": region,
            **label,
        },
        {
            "VolumeId": "vol-0demoattaching001",
            "VolumeState": "in-use",
            "AttachmentState": "Attaching",
            "InstanceId": "i-0demo9z8y7x6w5v4u3",
            "AttachmentCount": 1,
            "AvailabilityZone": f"{secondary}a",
            "SizeGiB": 200,
            "Region": secondary,
            **label,
        },
        {
            "VolumeId": "vol-0demodetaching001",
            "VolumeState": "in-use",
            "AttachmentState": "Detaching",
            "InstanceId": "i-0demo1a2b3c4d5e6f",
            "AttachmentCount": 1,
            "AvailabilityZone": f"{secondary}b",
            "SizeGiB": 500,
            "Region": secondary,
            **label,
        },
        {
            "VolumeId": "vol-0demomultiattach01",
            "VolumeState": "in-use",
            "AttachmentState": "Attached",
            "InstanceId": "i-0demo1a2b3c4d5e6f",
            "AttachmentCount": 2,
            "AvailabilityZone": f"{region}a",
            "SizeGiB": 1024,
            "Region": region,
            **label,
        },
        {
            "VolumeId": "vol-0demodeleting0001",
            "VolumeState": "deleting",
            "AttachmentState": "Not attached: Deleting",
            "InstanceId": None,
            "AttachmentCount": 0,
            "AvailabilityZone": f"{region}d",
            "SizeGiB": 100,
            "Region": region,
            **label,
        },
    ]
