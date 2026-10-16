"""
Unit tests for lambda/auto_tagger/main.py
"""
import json
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from tests.lambda_loader import load_lambda_module

auto_tagger = load_lambda_module("auto_tagger", "auto_tagger_main")

DEFAULT_TAGS = {"Environment": "Untagged", "Owner": "Unknown"}


def _ec2_with(instances, volumes):
    """The handler describes instances first, then volumes, off one client."""
    ec2 = MagicMock()
    ec2.get_paginator.return_value.paginate.side_effect = [
        [{"Reservations": [{"Instances": instances}]}],
        [{"Volumes": volumes}],
    ]
    return ec2


class TestApplyMissingTags:

    def test_adds_only_missing_keys(self):
        ec2 = MagicMock()
        added = auto_tagger.apply_missing_tags(ec2, "i-1", {"Environment"}, DEFAULT_TAGS)
        assert added is True
        tags = ec2.create_tags.call_args.kwargs["Tags"]
        assert tags == [{"Key": "Owner", "Value": "Unknown"}]

    def test_fully_tagged_resource_untouched(self):
        ec2 = MagicMock()
        added = auto_tagger.apply_missing_tags(
            ec2, "i-1", {"Environment", "Owner"}, DEFAULT_TAGS
        )
        assert added is False
        ec2.create_tags.assert_not_called()

    def test_client_error_does_not_raise(self):
        ec2 = MagicMock()
        ec2.create_tags.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation"}}, "CreateTags"
        )
        added = auto_tagger.apply_missing_tags(ec2, "i-1", set(), DEFAULT_TAGS)
        assert added is False


class TestTagUntaggedInstances:

    def _ec2_with_instances(self, instances):
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {"Reservations": [{"Instances": instances}]}
        ]
        return ec2

    def test_tags_untagged_instance(self):
        ec2 = self._ec2_with_instances([{"InstanceId": "i-1", "Tags": []}])
        tagged = auto_tagger.tag_untagged_instances(ec2, DEFAULT_TAGS)
        assert tagged == ["i-1"]
        ec2.create_tags.assert_called_once()

    def test_existing_tag_values_never_overwritten(self):
        ec2 = self._ec2_with_instances([
            {"InstanceId": "i-1", "Tags": [{"Key": "Environment", "Value": "prod"}]}
        ])
        auto_tagger.tag_untagged_instances(ec2, DEFAULT_TAGS)
        tags = ec2.create_tags.call_args.kwargs["Tags"]
        assert {"Key": "Environment", "Value": "Untagged"} not in tags
        assert {"Key": "Owner", "Value": "Unknown"} in tags

    def test_one_failure_does_not_stop_the_run(self):
        ec2 = self._ec2_with_instances([
            {"InstanceId": "i-bad", "Tags": []},
            {"InstanceId": "i-good", "Tags": []},
        ])
        ec2.create_tags.side_effect = [
            ClientError({"Error": {"Code": "InvalidID"}}, "CreateTags"),
            {},
        ]
        tagged = auto_tagger.tag_untagged_instances(ec2, DEFAULT_TAGS)
        assert tagged == ["i-good"]


class TestTagUntaggedVolumes:

    def test_tags_untagged_volume(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {"Volumes": [{"VolumeId": "vol-1", "Tags": []}]}
        ]
        tagged = auto_tagger.tag_untagged_volumes(ec2, DEFAULT_TAGS)
        assert tagged == ["vol-1"]

    def test_fully_tagged_volume_skipped(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {"Volumes": [{
                "VolumeId": "vol-1",
                "Tags": [{"Key": "Environment", "Value": "prod"},
                         {"Key": "Owner", "Value": "team-a"}],
            }]}
        ]
        tagged = auto_tagger.tag_untagged_volumes(ec2, DEFAULT_TAGS)
        assert tagged == []
        ec2.create_tags.assert_not_called()


class TestLambdaHandler:
    """The handler is where the DEFAULT_TAGS contract is parsed — the helper
    tests above inject a tags dict directly and never touch this path."""

    def _run(self, ec2):
        def client(service, **kwargs):
            return {"ec2": ec2, "sns": MagicMock()}[service]
        with patch.object(auto_tagger.boto3, "client", side_effect=client):
            return auto_tagger.lambda_handler({}, None)

    def test_tags_read_from_default_tags_env(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TAGS", json.dumps({"Team": "platform"}))
        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
        ec2 = _ec2_with([{"InstanceId": "i-1", "Tags": []}], [])
        result = self._run(ec2)
        assert result["statusCode"] == 200
        assert ec2.create_tags.call_args.kwargs["Tags"] == [{"Key": "Team", "Value": "platform"}]

    def test_builtin_defaults_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_TAGS", raising=False)
        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
        ec2 = _ec2_with([{"InstanceId": "i-1", "Tags": []}], [])
        self._run(ec2)
        assert ec2.create_tags.call_args.kwargs["Tags"] == [
            {"Key": "Environment", "Value": "Untagged"},
            {"Key": "Owner", "Value": "Unknown"},
        ]

    def test_body_reports_instances_and_volumes(self, monkeypatch):
        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
        ec2 = _ec2_with(
            [{"InstanceId": "i-1", "Tags": []}],
            [{"VolumeId": "vol-1", "Tags": []}],
        )
        result = self._run(ec2)
        body = json.loads(result["body"])
        assert body == {"Instances": ["i-1"], "Volumes": ["vol-1"]}


class TestPublishSummary:

    def test_skips_without_topic(self, monkeypatch):
        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
        with patch.object(auto_tagger.boto3, "client") as client:
            auto_tagger.publish_summary({"Instances": ["i-1"]}, 1)
        client.assert_not_called()

    def test_skips_when_nothing_was_tagged(self, monkeypatch):
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        with patch.object(auto_tagger.boto3, "client") as client:
            auto_tagger.publish_summary({"Instances": [], "Volumes": []}, 0)
        client.assert_not_called()

    def test_publishes_when_configured(self, monkeypatch):
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        sns = MagicMock()
        with patch.object(auto_tagger.boto3, "client", return_value=sns):
            auto_tagger.publish_summary({"Instances": ["i-1"], "Volumes": []}, 1)
        sns.publish.assert_called_once()
        assert "1 resource(s)" in sns.publish.call_args.kwargs["Subject"]

    def test_sns_failure_does_not_raise(self, monkeypatch):
        """A notification failure must not fail a run that already tagged
        resources successfully."""
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:topic")
        sns = MagicMock()
        sns.publish.side_effect = RuntimeError("sns down")
        with patch.object(auto_tagger.boto3, "client", return_value=sns):
            auto_tagger.publish_summary({"Instances": ["i-1"]}, 1)
