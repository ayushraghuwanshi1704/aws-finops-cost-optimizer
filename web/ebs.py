"""
Live EBS volume status for the dashboard.

One read-only EC2 operation backs this whole module: `DescribeVolumes`. Nothing
here creates, attaches, detaches, modifies or deletes a resource, and no other
API is called - `tests/test_ebs.py::TestEbsAccess::test_only_describe_volumes_is_called`
pins that down the way the Cost Explorer layer is pinned.

Labels come from what AWS actually returned rather than from inference. A volume
is called **Detached** only when its own state is `available` and it carries no
attachments, and **Attached** only when an attachment in `attached` state is
present. `creating`, `deleting`, `deleted`, `error`, `in-use` and any state AWS
introduces later are reported by that state, so an unknown shape is never
silently sorted into "attached" or "detached".

Volumes are regional, so the request carries a list of regions; each one is
queried separately and a region that fails is reported as its own error while
the regions that answered keep their real rows.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from web import live

# A hand-typed region list is capped so one dashboard poll cannot fan out to
# every region in the partition.
MAX_REGIONS = 8

# Attachment states EC2 documents, strongest evidence first: a multi-attach
# volume can report more than one attachment at once.
ATTACHMENT_LABELS: Dict[str, str] = {
    "attached": "Attached",
    "attaching": "Attaching",
    "detaching": "Detaching",
}
ATTACHMENT_PRIORITY: Tuple[str, ...] = ("attached", "attaching", "detaching")

# Volume states EC2 documents that carry no attachment. `available` is the one
# that means "detached"; the rest are named after their own state because
# calling a `creating` volume detached would be a claim AWS did not make.
DETACHED_STATES: Dict[str, str] = {
    "available": "Detached",
    "creating": "Not attached: Creating",
    "deleting": "Not attached: Deleting",
    "deleted": "Not attached: Deleted",
    "error": "Not attached: Error",
    "optimizing": "Not attached: Optimizing",
    # in-use means attached, so an empty attachment list is a real inconsistency
    # in the response and has to be shown as one.
    "in-use": "In use, but no attachment was returned",
}

ACCESS_DENIED_CODES = ("accessdenied", "unauthorizedoperation", "forsafe",
                       "client.unauthorized", "forbidden")
THROTTLE_CODES = ("requestlimitexceeded", "throttling", "toomanyrequests")
CLIENT_CODES = ("nocredentialserror", "credentialprovidererror", "profilenotfound",
                "missingdependencyerror", "missingdependencyexception", "noregionerror",
                "partialcredentials")

EBS_ACCESS_HINT = (
    "This identity is not allowed to call ec2:DescribeVolumes. Attach the "
    "read-only ec2:DescribeVolumes permission to the profile the server uses "
    "(see the IAM note in README.md), or present in Demo mode."
)
EBS_THROTTLE_HINT = (
    "EC2 throttled this DescribeVolumes request. Wait a moment and press "
    "Refresh volumes."
)
EBS_CLIENT_HINT = (
    "This process could not build an EC2 client - no usable AWS credential "
    "profile, or a required botocore dependency is missing. Run the server from "
    "the repository virtualenv, or choose Demo to keep presenting; volume "
    "statuses cannot be invented."
)


def _hint_for(code: str) -> str:
    """
    Advice for one AWS error code, worded for EC2.

    `DescribeVolumes` fails with the same credential codes as Cost Explorer, and
    those hints name no service, so they are shared. The three that do name Cost
    Explorer - access denied, throttling and client construction - are answered
    here instead, because telling a presenter to fix a Cost Explorer policy does
    not explain a failed EC2 read.
    """
    lowered = code.lower()
    if any(token in lowered for token in ACCESS_DENIED_CODES):
        return EBS_ACCESS_HINT
    if any(token in lowered for token in THROTTLE_CODES):
        return EBS_THROTTLE_HINT
    if any(token in lowered for token in CLIENT_CODES):
        return EBS_CLIENT_HINT
    return live.hint_for(code)


def error_from(exc: Exception) -> live.LiveError:
    """Type a botocore failure the same way the Cost Explorer layer does."""
    error = getattr(exc, "response", None) or {}
    code = (error.get("Error", {}) or {}).get("Code") or type(exc).__name__
    typed = live.error_from(exc)
    typed.hint = _hint_for(str(code))
    return typed


def make_client(region: str) -> Any:
    """
    Build a regional EC2 client, or explain why it could not be built.

    Credentials resolve at construction, so an unusable profile or a missing
    botocore dependency surfaces here with its own code instead of on the first
    query. The profile is never chosen by this module: the process environment
    (AWS_PROFILE, or whatever the server was started with) decides it.
    """
    try:
        import boto3

        return boto3.client("ec2", region_name=region)
    except Exception as exc:  # botocore raises a family of these
        raise error_from(exc)


def _humanize(state: str) -> str:
    return state.replace("-", " ").strip().title() if state else ""


def attachment_label(state: str, attachments: List[Dict]) -> str:
    """
    The attachment column for one volume, from its state and attachment list.

    Every branch that cannot be named from EC2's documented values keeps the raw
    AWS value in the text, so a new state reads as itself rather than as the
    closest familiar label.
    """
    normalized = str(state or "").strip().lower()

    if attachments:
        states = [str(item.get("State", "")).strip().lower() for item in attachments]
        for preferred in ATTACHMENT_PRIORITY:
            if preferred in states:
                return ATTACHMENT_LABELS[preferred]
        first = next((item for item in states if item), "")
        return f"Attachment state: {_humanize(first) or 'not returned'}"

    if normalized in DETACHED_STATES:
        return DETACHED_STATES[normalized]
    if not normalized:
        return "Volume state not returned"
    return f"Not attached: {_humanize(normalized)}"


def _attachment_priority(attachment: Dict) -> int:
    state = str(attachment.get("State", "")).strip().lower()
    return ATTACHMENT_PRIORITY.index(state) if state in ATTACHMENT_PRIORITY else len(
        ATTACHMENT_PRIORITY)


def _instance_id(attachments: List[Dict]) -> Optional[str]:
    """
    The instance to name in the table.

    A multi-attach volume lists several instances; the attached one is shown
    first because that is the attachment whose state the label reports.
    """
    for attachment in sorted(attachments, key=_attachment_priority):
        instance = str(attachment.get("InstanceId", "") or "").strip()
        if instance:
            return instance
    return None


def record_from(volume: Dict, region: str) -> Dict[str, Any]:
    """One EC2 volume description, in the seven columns the dashboard shows."""
    attachments = list(volume.get("Attachments") or [])
    state = str(volume.get("State", "") or "").strip().lower()
    size = volume.get("Size")
    return {
        "VolumeId": str(volume.get("VolumeId", "") or "(no volume id returned)"),
        "VolumeState": state or "(not returned)",
        "AttachmentState": attachment_label(state, attachments),
        "InstanceId": _instance_id(attachments),
        "AttachmentCount": len(attachments),
        "AvailabilityZone": str(volume.get("AvailabilityZone", "") or "(not returned)"),
        "SizeGiB": int(size) if isinstance(size, (int, float)) else None,
        "Region": region,
    }


def describe_volumes(client: Any, region: str) -> List[Dict[str, Any]]:
    """
    Every volume in one region, all pages merged.

    No status filter is sent: the table has to show attached volumes too, and
    filtering to `available` would hide exactly the rows that prove a volume is
    in use.
    """
    records: List[Dict[str, Any]] = []
    try:
        paginator = client.get_paginator("describe_volumes")
        for page in paginator.paginate():
            for volume in page.get("Volumes", []):
                records.append(record_from(volume, region))
    except (ClientError, BotoCoreError) as exc:
        raise error_from(exc)
    records.sort(key=lambda record: (record["Region"], record["VolumeId"]))
    return records


def _error_entry(region: str, exc: Exception) -> Dict[str, Any]:
    typed = error_from(exc) if not isinstance(exc, live.LiveError) else exc
    return {
        "Section": "ebs volumes",
        "Region": region,
        "Code": str(getattr(typed, "code", type(typed).__name__)),
        "Message": str(getattr(typed, "message", "") or str(typed)),
        "Hint": str(getattr(typed, "hint", "") or ""),
    }


def collect(regions: List[str]) -> Dict[str, Any]:
    """
    Query each requested region independently and keep what answered.

    A region that fails produces one error entry naming that region, never a
    fabricated volume list, so the table can show real rows beside a failed
    region and say which is which.
    """
    volumes: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    queried: List[str] = []

    for region in regions:
        try:
            client = make_client(region)
        except Exception as exc:  # credential/profile/dependency failures
            errors.append(_error_entry(region, exc))
            continue
        try:
            volumes.extend(describe_volumes(client, region))
            queried.append(region)
        except Exception as exc:  # access denied, throttling, region unreachable
            errors.append(_error_entry(region, exc))

    return {
        "Volumes": volumes,
        "Errors": errors,
        "RegionsRequested": list(regions),
        "RegionsQueried": queried,
        "FetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def status_for(mode: str, errors: List[Dict], volumes: List[Dict]) -> Dict[str, str]:
    """
    Say whether the volume list is live data, and what kind.

    An empty list with no errors is AWS reporting no volumes at all, which is a
    different fact from a scan that never ran; the table must not blur them.
    """
    if mode == "demo":
        return {
            "State": "demo",
            "Message": "Demo volumes - labelled sample rows, no AWS API was called.",
        }
    if errors and not volumes:
        return {
            "State": "unavailable",
            "Message": "EBS volume status unavailable - no region answered.",
        }
    if errors:
        return {
            "State": "partial",
            "Message": (
                f"EBS volume status partially connected - {len(volumes)} volume(s) "
                f"read, {len(errors)} region(s) could not be queried."
            ),
        }
    if not volumes:
        return {
            "State": "connected-empty",
            "Message": (
                "EBS volume status connected - AWS returned no volumes for these "
                "regions, so none are listed."
            ),
        }
    return {
        "State": "connected",
        "Message": f"EBS volume status connected - {len(volumes)} volume(s) read.",
    }
