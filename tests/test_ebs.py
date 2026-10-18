"""
Tests for the live EBS volume status feature (web/ebs.py, its demo rows and the
/api/ebs route).

Every AWS-facing class is patched, so no test can reach an account even on a
machine with working credentials, and a client stand-in records which operations
were requested - which is how the read-only guarantee is proven rather than
asserted in prose. The label tests answer the question the panel exists to
answer: does this row say what AWS said, or something nearby?
"""
import json
import re
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from cost_analysis import DEMO_WARNING
from web import app, data, demo_data, ebs, live


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query(raw: str = ""):
    return parse_qs(raw)


def _attachment(instance: str = "i-0sample", state: str = "attached",
                volume: str = "vol-0sample") -> Dict[str, str]:
    """One DescribeVolumes attachment entry, as EC2 returns it."""
    return {"AttachTime": "2000-01-01T00:00:00+00:00", "Device": "/dev/xvdf",
            "InstanceId": instance, "State": state, "VolumeId": volume}


def _volume(volume_id: str = "vol-0sample", state: str = "in-use",
            attachments: List[Dict] = None, size: Any = 8,
            zone: str = "us-east-1a", **overrides) -> Dict[str, Any]:
    """One DescribeVolumes volume entry, in the shape the API sends."""
    volume: Dict[str, Any] = {
        "Attachments": list(attachments or []),
        "AvailabilityZone": zone,
        "CreateTime": "2000-01-01T00:00:00+00:00",
        "Encrypted": False,
        "MultiAttachEnabled": len(attachments or []) > 1,
        "Size": size,
        "State": state,
        "Tags": [],
        "VolumeId": volume_id,
    }
    volume.update(overrides)
    return volume


def _fake_ec2(pages: List[Dict] = None, error: Exception = None) -> MagicMock:
    """
    An EC2 client stand-in that answers DescribeVolumes.

    The paginator is the only path `web/ebs.py` uses, and every `paginate()` call
    is recorded so a test can read the exact parameters that were sent.
    """
    calls: List[Dict] = []
    client = MagicMock()
    client.paginate_calls = calls

    def paginate(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return list(pages or [])

    client.get_paginator.return_value.paginate.side_effect = paginate
    return client


def _page(volumes: List[Dict]) -> Dict[str, List[Dict]]:
    return {"Volumes": volumes}


def _client_error(code: str, message: str = "api detail") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "DescribeVolumes")


def _clients_by_region(clients: Dict[str, Any]):
    """A `make_client` stand-in that hands back the prepared client for a region."""
    def build(region: str):
        if region not in clients:
            raise AssertionError(f"collect() queried an unstubbed region: {region}")
        return clients[region]
    return build


def _volumes_by_region(regions: Dict[str, List[Dict]], errors: Dict[str, Exception] = None):
    """
    Patch `web.ebs.make_client` so each region answers from `regions`.

    A region listed in `errors` fails instead, which is how a partly-reachable
    region list is exercised without a real account.
    """
    clients = {}
    for region, volumes in regions.items():
        clients[region] = _fake_ec2([_page(volumes)], (errors or {}).get(region))
    for region, exc in (errors or {}).items():
        clients.setdefault(region, _fake_ec2([], exc))
    return patch("web.ebs.make_client", side_effect=_clients_by_region(clients))


def _get_ebs(query: str = ""):
    status, headers, body = app.dispatch("GET", "/api/ebs", _query(query))
    return status, headers, json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Labels: what AWS said, not what is closest
# ---------------------------------------------------------------------------

class TestAttachmentLabels:

    def test_available_with_no_attachment_is_detached(self):
        """This is EC2's own definition of a detached volume."""
        assert ebs.attachment_label("available", []) == "Detached"

    def test_an_attached_attachment_is_attached(self):
        label = ebs.attachment_label("in-use", [_attachment(state="attached")])
        assert label == "Attached"

    def test_a_transitional_attachment_is_named_after_its_own_state(self):
        assert ebs.attachment_label("in-use", [_attachment(state="attaching")]) == "Attaching"
        assert ebs.attachment_label("in-use", [_attachment(state="detaching")]) == "Detaching"

    def test_non_attached_states_are_not_called_detached(self):
        """
        Only `available` means detached.

        A creating, deleting, deleted or error volume is not simply unattached -
        it is in a state of its own, and the panel has to say which one.
        """
        expected = {
            "creating": "Not attached: Creating",
            "deleting": "Not attached: Deleting",
            "deleted": "Not attached: Deleted",
            "error": "Not attached: Error",
            "optimizing": "Not attached: Optimizing",
        }
        for state, label in expected.items():
            assert ebs.attachment_label(state, []) == label, state

    def test_in_use_without_an_attachment_reads_as_inconsistent(self):
        """`in-use` means attached, so an empty list is a real contradiction."""
        assert ebs.attachment_label("in-use", []) == "In use, but no attachment was returned"

    def test_an_unknown_volume_state_is_not_sorted_into_attached_or_detached(self):
        """EC2 adds states; a row must never be guessed into the familiar pair."""
        for state in ("quiescing", "modifying", "initializing", "some-future-state"):
            label = ebs.attachment_label(state, [])
            assert label not in ("Attached", "Detached"), state
            assert state.replace("-", " ").title() in label, label

    def test_an_unknown_attachment_state_keeps_the_value_aws_sent(self):
        label = ebs.attachment_label("in-use", [_attachment(state="maintenance")])
        assert label == "Attachment state: Maintenance"

    def test_an_attachment_without_a_state_says_so(self):
        assert ebs.attachment_label("in-use", [{"InstanceId": "i-1"}]) == \
            "Attachment state: not returned"

    def test_a_volume_without_a_state_reports_that_it_is_missing(self):
        assert ebs.attachment_label("", []) == "Volume state not returned"
        assert ebs.attachment_label(None, []) == "Volume state not returned"

    def test_states_are_matched_case_insensitively(self):
        """The table keys on one casing; AWS is compared after normalising."""
        assert ebs.attachment_label("AVAILABLE", []) == "Detached"
        assert ebs.attachment_label("In-Use", [_attachment(state=" ATTACHED ")]) == "Attached"

    def test_multi_attach_shows_the_attached_instance_first(self):
        """
        A multi-attach volume reports several attachments at once.

        The label follows the strongest evidence and the named instance has to be
        the one the label describes, not whichever EC2 happened to list first.
        """
        attachments = [_attachment(instance="i-detaching", state="detaching"),
                       _attachment(instance="i-attached", state="attached")]
        assert ebs.attachment_label("in-use", attachments) == "Attached"
        assert ebs._instance_id(attachments) == "i-attached"

    def test_attaching_outranks_detaching(self):
        attachments = [_attachment(instance="i-old", state="detaching"),
                       _attachment(instance="i-new", state="attaching")]
        assert ebs.attachment_label("in-use", attachments) == "Attaching"
        assert ebs._instance_id(attachments) == "i-new"

    def test_an_instance_id_is_only_reported_when_an_attachment_exists(self):
        assert ebs._instance_id([]) is None
        assert ebs._instance_id([{"State": "attached"}]) is None


# ---------------------------------------------------------------------------
# Record shape: the seven columns the table shows
# ---------------------------------------------------------------------------

class TestVolumeRecord:

    def test_record_carries_the_dashboard_columns(self):
        record = ebs.record_from(_volume(volume_id="vol-1", state="in-use",
                                         attachments=[_attachment("i-1")], size=500,
                                         zone="ap-south-1a"), "ap-south-1")
        assert record == {
            "VolumeId": "vol-1",
            "VolumeState": "in-use",
            "AttachmentState": "Attached",
            "InstanceId": "i-1",
            "AttachmentCount": 1,
            "AvailabilityZone": "ap-south-1a",
            "SizeGiB": 500,
            "Region": "ap-south-1",
        }

    def test_a_detached_volume_has_no_instance(self):
        record = ebs.record_from(_volume(state="available", attachments=None), "us-east-1")
        assert record["AttachmentState"] == "Detached"
        assert record["InstanceId"] is None
        assert record["AttachmentCount"] == 0

    def test_size_is_whole_gib_or_missing_never_zero(self):
        assert ebs.record_from(_volume(size=8), "us-east-1")["SizeGiB"] == 8
        assert ebs.record_from(_volume(size=None), "us-east-1")["SizeGiB"] is None
        assert ebs.record_from(_volume(size="8"), "us-east-1")["SizeGiB"] is None

    def test_absent_fields_are_marked_not_returned(self):
        """A row cannot invent an id, a zone or a state AWS omitted."""
        record = ebs.record_from({}, "eu-west-1")
        assert record["VolumeId"] == "(no volume id returned)"
        assert record["VolumeState"] == "(not returned)"
        assert record["AvailabilityZone"] == "(not returned)"
        assert record["SizeGiB"] is None
        assert record["Region"] == "eu-west-1"

    def test_attachment_count_shows_how_many_instances_see_the_volume(self):
        attachments = [_attachment("i-1", "attached"), _attachment("i-2", "attached")]
        record = ebs.record_from(_volume(attachments=attachments), "us-east-1")
        assert record["AttachmentCount"] == 2
        assert record["AttachmentState"] == "Attached"


# ---------------------------------------------------------------------------
# The API call itself
# ---------------------------------------------------------------------------

class TestEbsAccess:

    def test_only_describe_volumes_is_called(self):
        """Read-only guarantee: this layer never asks EC2 for a mutating action."""
        client = _fake_ec2([_page([_volume()])])
        ebs.describe_volumes(client, "us-east-1")
        assert {call[0] for call in client.method_calls} == {"get_paginator"}
        assert [call[0] for call in client.get_paginator.call_args_list] == \
            [("describe_volumes",)]

    def test_no_status_filter_is_sent(self):
        """
        Every volume is listed, attached ones included.

        `scripts/unused_resources.py` filters to `available` because it hunts for
        waste; this panel answers "what is the state of every volume", so a
        filter would hide exactly the rows that prove a volume is in use.
        """
        client = _fake_ec2([_page([_volume()])])
        ebs.describe_volumes(client, "us-east-1")
        assert client.paginate_calls == [{}], client.paginate_calls

    def test_every_page_is_merged(self):
        """DescribeVolumes paginates; a partial merge would understate the list."""
        client = _fake_ec2([_page([_volume("vol-b")]), _page([_volume("vol-a")])])
        records = ebs.describe_volumes(client, "us-east-1")
        assert [record["VolumeId"] for record in records] == ["vol-a", "vol-b"]

    def test_records_are_sorted_by_region_then_volume(self):
        first = ebs.describe_volumes(_fake_ec2([_page([_volume("vol-z")])]), "us-east-2")
        second = ebs.describe_volumes(_fake_ec2([_page([_volume("vol-a")])]), "eu-west-1")
        combined = sorted(first + second, key=lambda r: (r["Region"], r["VolumeId"]))
        assert [(r["Region"], r["VolumeId"]) for r in combined] == \
            [("eu-west-1", "vol-a"), ("us-east-2", "vol-z")]

    def test_an_empty_region_returns_no_rows_rather_than_an_error(self):
        assert ebs.describe_volumes(_fake_ec2([{}]), "us-east-1") == []
        assert ebs.describe_volumes(_fake_ec2([]), "us-east-1") == []

    def test_a_failure_keeps_the_code_and_names_the_ec2_action(self):
        client = _fake_ec2(error=_client_error("UnauthorizedOperation",
                                               "not authorized to describe volumes"))
        try:
            ebs.describe_volumes(client, "us-east-1")
            raise AssertionError("describe_volumes should re-raise as a LiveError")
        except live.LiveError as exc:
            assert exc.code == "UnauthorizedOperation"
            assert "ec2:DescribeVolumes" in exc.hint
            assert "Cost Explorer" not in exc.hint


# ---------------------------------------------------------------------------
# Hints an EC2 presenter can act on
# ---------------------------------------------------------------------------

class TestEbsHints:

    def test_each_failure_code_is_advised_for_ec2_not_cost_explorer(self):
        """
        The shared table's wording names the billing API.

        A DescribeVolumes failure has to be explained in EC2 terms, or the reader
        goes looking in the wrong policy.
        """
        cases = {
            "AccessDenied": "ec2:DescribeVolumes",
            "UnauthorizedOperation": "ec2:DescribeVolumes",
            "RequestLimitExceeded": "EC2 throttled",
            "ThrottlingException": "EC2 throttled",
            "NoCredentialsError": "could not build an EC2 client",
            "ProfileNotFound": "could not build an EC2 client",
            "MissingDependencyException": "repository virtualenv",
            # The code the `login` credential provider really returns once the
            # SSO session behind the profile has run out.
            "LoginRefreshRequired": "expired session token",
        }
        for code, needle in cases.items():
            hint = ebs._hint_for(code)
            assert needle in hint, f"{code} hint: {hint!r}"
            assert "Cost Explorer" not in hint, f"{code} hint: {hint!r}"

    def test_a_credential_rejection_is_shared_because_it_names_no_service(self):
        """An expired SSO token is the same advice for any API."""
        assert ebs._hint_for("InvalidClientTokenId") == live.hint_for("InvalidClientTokenId")

    def test_an_unclassified_code_is_still_carried_in_full(self):
        typed = ebs.error_from(_client_error("SomeNewEC2Code", "line one\nline two"))
        assert typed.code == "SomeNewEC2Code"
        assert typed.message.startswith("ClientError (SomeNewEC2Code): line one")
        assert typed.hint == ""

    def test_a_client_that_cannot_be_built_is_typed_too(self):
        """botocore raises outside the request path, and the code still has to read."""
        typed = ebs.error_from(NoCredentialsError())
        assert typed.code == "NoCredentialsError"
        assert "ec2" in typed.hint.lower()


# ---------------------------------------------------------------------------
# collect(): a region list, answered one region at a time
# ---------------------------------------------------------------------------

class TestCollect:

    def test_a_failed_region_keeps_the_regions_that_answered(self):
        """Partial data must stay visible, labelled as partial - never replaced."""
        with _volumes_by_region(
            {"us-east-1": [_volume("vol-good", "available", None)],
             "eu-west-1": []},
            errors={"eu-west-1": _client_error("AccessDenied")},
        ):
            result = ebs.collect(["us-east-1", "eu-west-1"])

        assert [row["VolumeId"] for row in result["Volumes"]] == ["vol-good"]
        assert result["RegionsQueried"] == ["us-east-1"]
        assert result["RegionsRequested"] == ["us-east-1", "eu-west-1"]
        assert len(result["Errors"]) == 1
        assert result["Errors"][0]["Region"] == "eu-west-1"
        assert result["Errors"][0]["Code"] == "AccessDenied"
        assert result["Errors"][0]["Section"] == "ebs volumes"
        assert "ec2:DescribeVolumes" in result["Errors"][0]["Hint"]
        assert ebs.status_for("live", result["Errors"], result["Volumes"])["State"] == "partial"

    def test_no_region_answering_leaves_nothing_to_present(self):
        with _volumes_by_region({"us-east-1": []},
                                errors={"us-east-1": _client_error("RequestLimitExceeded")}):
            result = ebs.collect(["us-east-1"])

        assert result["Volumes"] == []
        assert result["RegionsQueried"] == []
        assert ebs.status_for("live", result["Errors"], [])["State"] == "unavailable"

    def test_a_profile_that_builds_no_client_is_an_error_per_region(self):
        """
        Credentials resolve at client construction, so this fails before EC2 is
        reached. The panel has to report that, not an empty account.
        """
        with patch("web.ebs.make_client", side_effect=NoCredentialsError()):
            result = ebs.collect(["us-east-1", "eu-west-1"])

        assert result["Volumes"] == []
        assert [error["Code"] for error in result["Errors"]] == \
            ["NoCredentialsError", "NoCredentialsError"]
        assert "EC2 client" in result["Errors"][0]["Hint"]

    def test_an_expired_sso_session_is_reported_as_a_credential_problem(self):
        """
        The failure this host actually produces: credentials resolve, then AWS
        rejects them. That is an unavailable panel, never an empty account.
        """
        class LoginRefreshRequired(Exception):
            """Stand-in for the exception the `login` credential provider raises."""

        with patch("web.ebs.make_client", side_effect=LoginRefreshRequired(
                "Your session has expired. Please reauthenticate using 'aws login'.")):
            result = ebs.collect(["us-east-1"])

        assert result["Volumes"] == []
        assert result["RegionsQueried"] == []
        error = result["Errors"][0]
        assert error["Code"] == "LoginRefreshRequired"
        assert "session has expired" in error["Message"]
        assert "expired session token" in error["Hint"]
        assert ebs.status_for("live", result["Errors"], [])["State"] == "unavailable"

    def test_an_empty_account_is_not_reported_as_a_failure(self):
        with _volumes_by_region({"us-east-1": []}):
            result = ebs.collect(["us-east-1"])

        assert result["Volumes"] == []
        assert result["Errors"] == []
        assert result["RegionsQueried"] == ["us-east-1"]
        assert ebs.status_for("live", [], result["Volumes"])["State"] == "connected-empty"

    def test_the_scan_is_timestamped_once_for_the_request(self):
        with _volumes_by_region({"us-east-1": [_volume("vol-a")], "eu-west-1": []}):
            result = ebs.collect(["us-east-1", "eu-west-1"])

        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", result["FetchedAt"])

    def test_each_region_is_queried_with_its_own_client(self):
        """Volumes are regional; one client for two regions would silently drop rows."""
        with _volumes_by_region({"us-east-1": [_volume("vol-a")],
                                 "ap-south-1": [_volume("vol-b", zone="ap-south-1a")]}) as mock:
            result = ebs.collect(["us-east-1", "ap-south-1"])

        assert [call[0][0] for call in mock.call_args_list] == ["us-east-1", "ap-south-1"]
        assert {row["Region"] for row in result["Volumes"]} == {"us-east-1", "ap-south-1"}


class TestEbsStatus:

    def test_demo_rows_are_never_described_as_live(self):
        status = ebs.status_for("demo", [], demo_data.demo_volumes(["us-east-1"]))
        assert status["State"] == "demo"
        assert "no AWS API was called" in status["Message"]

    def test_the_connected_message_counts_the_volumes_read(self):
        volumes = [_volume("vol-a"), _volume("vol-b"), _volume("vol-c")]
        status = ebs.status_for("live", [], volumes)
        assert status["State"] == "connected"
        assert "3 volume(s) read" in status["Message"]

    def test_partial_names_both_the_rows_and_the_failed_regions(self):
        errors = [{"Region": "eu-west-1", "Code": "AccessDenied",
                   "Message": "no", "Hint": "no"}]
        status = ebs.status_for("live", errors, [_volume("vol-a")])
        assert status["State"] == "partial"
        assert "1 volume(s) read" in status["Message"]
        assert "1 region(s) could not be queried" in status["Message"]

    def test_an_empty_answer_says_no_volumes_rather_than_no_data(self):
        message = ebs.status_for("live", [], [])["Message"]
        assert "returned no volumes" in message

    def test_unavailable_says_no_region_answered(self):
        errors = [{"Region": "us-east-1", "Code": "NoCredentialsError",
                   "Message": "no", "Hint": "no"}]
        status = ebs.status_for("live", errors, [])
        assert status["State"] == "unavailable"
        assert "no region answered" in status["Message"]


# ---------------------------------------------------------------------------
# Demo rows: the same shape, never passed off as real
# ---------------------------------------------------------------------------

class TestDemoVolumes:

    def test_every_demo_row_is_labelled_as_a_sample(self):
        volumes = demo_data.demo_volumes(["us-east-1", "eu-west-1"])
        assert volumes
        for row in volumes:
            assert row["Source"] == DEMO_WARNING, row

    def test_demo_rows_use_the_live_field_names(self):
        """One render path for both modes, so the table cannot drift per mode."""
        live_keys = set(ebs.record_from(_volume(), "us-east-1"))
        for row in demo_data.demo_volumes(["us-east-1"]):
            assert live_keys.issubset(set(row)), sorted(live_keys - set(row))

    def test_demo_rows_show_each_label_the_table_can_produce(self):
        labels = {row["AttachmentState"] for row in demo_data.demo_volumes(["us-east-1"])}
        for expected in ("Attached", "Detached", "Attaching", "Detaching",
                         "Not attached: Creating", "Not attached: Deleting"):
            assert expected in labels, expected

    def test_demo_rows_follow_the_requested_regions(self):
        volumes = demo_data.demo_volumes(["ap-south-1"])
        assert {row["Region"] for row in volumes} == {"ap-south-1"}
        assert {row["AvailabilityZone"] for row in volumes} <= {
            "ap-south-1a", "ap-south-1b", "ap-south-1c", "ap-south-1d"}

    def test_a_detached_demo_volume_has_the_waste_shape(self):
        """The row the unused-volume finder also charges for reads the same here."""
        detached = [row for row in demo_data.demo_volumes(["us-east-1"])
                    if row["AttachmentState"] == "Detached"]
        assert len(detached) == 1
        assert detached[0]["VolumeState"] == "available"
        assert detached[0]["InstanceId"] is None


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

class TestEbsPayload:

    def test_demo_payload_calls_no_aws_api(self):
        """Demo is the answer to a missing profile, so it must not need one."""
        with patch("web.ebs.make_client") as builder:
            payload = data.ebs_payload("demo", ["us-east-1"])

        assert builder.call_count == 0
        assert payload["meta"]["status"]["State"] == "demo"
        assert payload["errors"] == []
        assert payload["meta"]["regionsRequested"] == ["us-east-1"]
        assert "No AWS API was called" in payload["meta"]["dataSource"]

    def test_live_payload_reports_only_what_aws_returned(self):
        with _volumes_by_region({"us-east-1": [_volume("vol-1", "in-use",
                                                       [_attachment("i-1", "attached")]),
                                               _volume("vol-2", "available", None)]}):
            payload = data.ebs_payload("live", ["us-east-1"])

        assert payload["meta"]["status"]["State"] == "connected"
        assert [row["VolumeId"] for row in payload["volumes"]] == ["vol-1", "vol-2"]
        assert payload["errors"] == []
        assert payload["meta"]["regionsQueried"] == ["us-east-1"]
        assert "DescribeVolumes (read-only)" in payload["meta"]["dataSource"]

    def test_a_live_payload_never_contains_a_sample_volume(self):
        with _volumes_by_region({"us-east-1": [_volume("vol-real", "available", None)]}):
            payload = data.ebs_payload("live", ["us-east-1"])

        text = json.dumps(payload)
        assert DEMO_WARNING not in text
        assert "vol-0demo" not in text

    def test_the_payload_names_the_one_read_only_action_it_needs(self):
        with _volumes_by_region({"us-east-1": []}):
            payload = data.ebs_payload("live", ["us-east-1"])
        assert payload["meta"]["readOnlyAction"] == "ec2:DescribeVolumes"

    def test_the_scan_time_is_carried_out_for_the_refresh_label(self):
        with _volumes_by_region({"us-east-1": [_volume()]}):
            payload = data.ebs_payload("live", ["us-east-1"])
        assert re.match(r"^\d{4}-\d{2}-\d{2}T", payload["meta"]["fetchedAt"])

    def test_an_unusable_mode_or_region_list_is_refused_before_aws(self):
        with pytest.raises(ValueError):
            data.ebs_payload("production", ["us-east-1"])
        with pytest.raises(ValueError):
            data.ebs_payload("live", [])

    def test_the_cost_payload_is_untouched_by_the_volume_layer(self):
        """Billing must not grow a volumes key nobody asked it to carry."""
        payload = app.build_payload({"days": 30, "mode": "demo", "region": "us-east-1"})
        assert "volumes" not in payload
        assert payload["meta"]["mode"] == "demo"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

class TestEbsRoute:

    def test_demo_route_serves_labelled_rows(self):
        status, headers, payload = _get_ebs("mode=demo&regions=us-east-1,eu-west-1")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert payload["meta"]["mode"] == "demo"
        assert payload["meta"]["regionsRequested"] == ["us-east-1", "eu-west-1"]
        assert len(payload["volumes"]) == len(demo_data.demo_volumes(["us-east-1", "eu-west-1"]))
        assert all(row["Source"] == DEMO_WARNING for row in payload["volumes"])
        assert payload["meta"]["aws"]["apiCallsMade"] is False

    def test_a_partly_failed_live_scan_answers_207_not_200(self):
        """A region list that only half answered is not a clean report."""
        with _volumes_by_region({"us-east-1": [_volume("vol-good", "available", None)]},
                                errors={"eu-west-1": _client_error("AccessDenied")}):
            status, _, payload = _get_ebs("mode=live&regions=us-east-1,eu-west-1")

        assert status == 207
        assert payload["meta"]["status"]["State"] == "partial"
        assert [row["VolumeId"] for row in payload["volumes"]] == ["vol-good"]
        assert len(payload["errors"]) == 1

    def test_a_clean_live_scan_answers_200(self):
        with _volumes_by_region({"us-east-1": [_volume("vol-one")]}):
            status, _, payload = _get_ebs("mode=live&regions=us-east-1")
        assert status == 200
        assert payload["meta"]["status"]["State"] == "connected"

    def test_an_empty_live_account_is_a_clean_200(self):
        """AWS answering with no volumes is a successful read, not a failure."""
        with _volumes_by_region({"us-east-1": []}):
            status, _, payload = _get_ebs("mode=live&regions=us-east-1")
        assert status == 200
        assert payload["volumes"] == []
        assert payload["meta"]["status"]["State"] == "connected-empty"

    def test_an_unreachable_region_is_reported_not_invented(self):
        """
        A hand-typed region name reaches AWS and fails there.

        The honest answer is one error entry; a fabricated empty list would read
        as "this region has no volumes".
        """
        with _volumes_by_region({"no-such-region-9": []},
                                errors={"no-such-region-9": _client_error(
                                    "InvalidRequest", "The action DescribeVolumes is not valid")}):
            status, _, payload = _get_ebs("mode=live&regions=no-such-region-9")

        assert status == 207
        assert payload["volumes"] == []
        assert payload["errors"][0]["Region"] == "no-such-region-9"
        assert payload["meta"]["status"]["State"] == "unavailable"

    def test_a_demo_only_server_still_answers_in_demo(self):
        params = app.parse_params(_query("mode=live&regions=us-east-1"), allow_live=False)
        assert params["mode"] == "demo"

        status, _, raw = app.dispatch(
            "GET", "/api/ebs", _query("mode=live&regions=us-east-1"), allow_live=False)
        payload = json.loads(raw.decode("utf-8"))
        assert status == 200
        assert payload["meta"]["mode"] == "demo"
        assert payload["meta"]["status"]["State"] == "demo"
        assert all(row["Source"] == DEMO_WARNING for row in payload["volumes"])

    def test_the_volume_route_is_not_a_cost_route(self):
        """One DescribeVolumes poll must not pay for a Cost Explorer run."""
        with patch("web.data.build_payload") as cost, patch("web.ebs.make_client") as builder:
            cost.side_effect = AssertionError("/api/ebs must not build a cost payload")
            builder.side_effect = NoCredentialsError()
            status, _, payload = _get_ebs("mode=live&regions=us-east-1")
        assert status == 207
        assert payload["meta"]["status"]["State"] == "unavailable"


class TestRegionParameter:

    def test_comma_and_repeated_parameters_are_both_honoured(self):
        one = app.parse_regions(_query("regions=us-east-1,eu-west-1"), "us-east-1")
        many = app.parse_regions(_query("regions=us-east-1&regions=eu-west-1"), "us-east-1")
        assert one == many == ["us-east-1", "eu-west-1"]

    def test_the_list_is_deduplicated_and_lowercased(self):
        regions = app.parse_regions(_query("regions=US-EAST-1,us-east-1"), "us-east-1")
        assert regions == ["us-east-1"]

    def test_malformed_tokens_are_dropped_rather_than_guessed_at(self):
        for raw in ("regions=../etc", "regions=a", "regions=" + "z" * 40,
                    "regions=us-east-1%20rm", "regions=us-east-1;rm"):
            regions = app.parse_regions(_query(raw), "us-east-1")
            assert regions == ["us-east-1"] or all(re.match(r"^[a-z0-9-]{3,25}$", r)
                                                   for r in regions), raw

    def test_an_empty_or_absent_list_falls_back_to_the_dashboard_region(self):
        assert app.parse_regions(_query("regions="), "ap-south-1") == ["ap-south-1"]
        assert app.parse_regions(_query("regions=,,,"), "eu-west-1") == ["eu-west-1"]
        assert app.parse_regions({}, "us-west-2") == ["us-west-2"]

    def test_the_list_is_capped_so_one_poll_cannot_fan_out(self):
        raw = "regions=" + ",".join(f"us-east-1-r{i}" for i in range(30))
        regions = app.parse_regions(_query(raw), "us-east-1")
        assert len(regions) == ebs.MAX_REGIONS
        assert regions == regions[:ebs.MAX_REGIONS]

    def test_the_cap_is_applied_before_aws_is_reached(self):
        """`web/ebs.MAX_REGIONS` is the server's promise; the route must keep it."""
        seen = []

        def build(region):
            seen.append(region)
            raise NoCredentialsError()

        with patch("web.ebs.make_client", side_effect=build):
            _get_ebs("mode=live&regions=" + ",".join(
                f"region-{index:02d}a" for index in range(20)))
        assert len(seen) == ebs.MAX_REGIONS


# ---------------------------------------------------------------------------
# Shell and script wiring
# ---------------------------------------------------------------------------

class TestEbsFrontendWiring:

    def setup_method(self):
        _, _, index = app.dispatch("GET", "/")
        self.html = index.decode("utf-8")
        _, _, script = app.dispatch("GET", "/static/app.js")
        self.js = script.decode("utf-8")
        _, _, sheet = app.dispatch("GET", "/static/styles.css")
        self.css = sheet.decode("utf-8")

    def test_the_shell_has_a_volumes_view_with_the_seven_columns(self):
        assert 'data-view="volumes"' in self.html
        for header in ("Volume ID", "Volume state", "Attachment state", "Instance",
                       "Availability zone", "Size (GiB)", "Region"):
            assert header in self.html, header
        assert 'id="ebs-table"' in self.html

    def test_the_shell_has_a_refresh_button_a_region_field_and_a_time_label(self):
        for element in ('id="ebs-refresh"', 'id="ebs-regions"', 'id="ebs-status"',
                        'id="ebs-errors"', 'id="ebs-note"'):
            assert element in self.html, element
        assert 'role="status"' in self.html

    def test_the_panel_states_that_the_data_is_a_read_only_describe(self):
        assert "DescribeVolumes" in self.html
        assert "ec2:DescribeVolumes" in self.js

    def test_the_script_polls_the_volume_endpoint_on_a_timer(self):
        assert "/api/ebs" in self.js
        assert "setInterval" in self.js
        assert "EBS_REFRESH_MS" in self.js
        # Requirement: the last successful refresh time is shown.
        assert "Last refreshed" in self.js
        assert 'refreshEbs("manual")' in self.js
        assert "document.hidden" in self.js

    def test_a_failed_poll_is_shown_as_stale_rather_than_empty(self):
        assert "Refresh failed" in self.js
        assert "Showing the volume list from" in self.js

    def test_the_volume_panel_is_wired_on_page_load_and_by_its_own_controls(self):
        bootstrap = self.js.split("wireNavigation();")[-1]
        assert "wireEbs();" in bootstrap
        assert 'refreshEbs("initial")' in bootstrap
        handler = self.js.split("function wireEbs()")[1].split("wireNavigation();")[0]
        assert 'getElementById("ebs-refresh")' in handler
        assert 'getElementById("ebs-regions")' in handler
        assert 'refreshEbs("manual")' in handler
        assert 'refreshEbs("regions")' in handler

    def test_the_script_distinguishes_no_volumes_from_no_access(self):
        for needle in ("connected-empty", "unavailable", "partial",
                       "no region could be queried"):
            assert needle in self.js, needle

    def test_only_documented_labels_get_the_colour_coding(self):
        """
        The colour is a scanning cue for attached/detached, nothing else.

        A state AWS returns that is neither stays neutral instead of being dressed
        up as one of the two familiar ones, and the tag is a span because a td in a
        border-collapsed table ignores border-radius.
        """
        assert 'if (label === "Attached") return "is-attached"' in self.js
        assert 'if (label === "Detached") return "is-detached"' in self.js
        assert 'el("span", tag ? `tag ${tag}` : "tag", value)' in self.js
        assert "{ value: row.AttachmentState, tag: attachmentTag(row.AttachmentState) }" in self.js
        assert ".tag.is-attached" in self.css
        assert ".tag.is-detached" in self.css

    def test_the_volume_panel_adds_no_inline_script(self):
        """The CSP forbids inline script; this panel has to obey it like the rest."""
        assert "onclick=" not in self.html
        assert "<script>" not in self.html.replace("<script src=", "<script-src ")

    def test_switching_mode_clears_the_previous_volume_rows(self):
        """Sample rows must never sit under a live selection, or vice versa."""
        filters = self.js.split("function wireFilters()")[1].split("btn-refresh")[0]
        assert "state.ebs = null" in filters
        assert 'refreshEbs("mode")' in filters
