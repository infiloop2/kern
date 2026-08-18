from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
import errno
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from host.bootstrap import render
from host.config import ConfigError, build_input_config, build_operator_connections
from host.cli import lifecycle as deploy
from host.cli import lifecycle_aws
from host.cli import power
from host.runtime.core import db



class FakeScandir:
    def __init__(self, entries: list[object]) -> None:
        self._entries = entries

    def __enter__(self) -> FakeScandir:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Iterator[object]:
        return iter(self._entries)


SAMPLE_SSH_PUBLIC_KEY = "ssh-ed25519 AAAATEST operator@example"
SAMPLE_AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "AKIATEST",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "AWS_REGION": "us-east-1",
}


def sample_input_config():  # type: ignore[no-untyped-def]
    return build_input_config("kern-test", "us-east-1")


SAMPLE_ADMIN_PASSWORD_SHA256 = "f" * 64


def _fake_deploy_key(workdir: object) -> Path:
    key_path = Path(str(workdir)) / "deploy_key"
    key_path.write_text("fake-private-key")
    key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAADEPLOY kern-deploy")
    return key_path


class DeployUnitTests(unittest.TestCase):
    def test_aws_env_uses_standard_credentials_and_pins_region(self) -> None:
        config = sample_input_config()
        env_values = {
            "AWS_ACCESS_KEY_ID": "access",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "sts-token",
        }
        with patch.dict(os.environ, env_values, clear=False):
            env = lifecycle_aws._aws_env(config)
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "access")
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "secret")
        # A session token is used exactly when set; a stale one next to fresh
        # static keys fails closed at AWS instead of being silently dropped.
        self.assertEqual(env["AWS_SESSION_TOKEN"], "sts-token")
        self.assertEqual(env["AWS_REGION"], "us-east-1")
        self.assertEqual(env["AWS_DEFAULT_REGION"], "us-east-1")

    def test_aws_env_requires_standard_credentials(self) -> None:
        config = sample_input_config()
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "secret"}, clear=False):
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
            with self.assertRaisesRegex(ConfigError, "AWS_ACCESS_KEY_ID is not set"):
                lifecycle_aws._aws_env(config)

    def test_build_input_config_validates_name_and_region(self) -> None:
        config = build_input_config(" kern-test ", "us-east-1")
        self.assertEqual(config.agent_name, "kern-test")
        self.assertEqual(config.aws_region, "us-east-1")
        with self.assertRaisesRegex(ConfigError, "agent name must be"):
            build_input_config("bad name!", "us-east-1")
        with self.assertRaisesRegex(ConfigError, "AWS region must look like"):
            build_input_config("kern-test", "everywhere")

    def test_build_operator_connections_validates_and_requires_one(self) -> None:
        connections = build_operator_connections(SAMPLE_SSH_PUBLIC_KEY, None, None)
        self.assertEqual(connections[0].mode, "ssh")
        self.assertEqual(connections[0].ssh_public_key, SAMPLE_SSH_PUBLIC_KEY)
        connections = build_operator_connections(None, "Agent.Example.com", "token-value")
        self.assertEqual(connections[0].mode, "cloudflare_tunnel")
        self.assertEqual(connections[0].hostname, "agent.example.com")
        self.assertEqual(connections[0].tunnel_token, "token-value")
        both = build_operator_connections(SAMPLE_SSH_PUBLIC_KEY, "agent.example.com", "token-value")
        self.assertEqual([connection.mode for connection in both], ["ssh", "cloudflare_tunnel"])
        with self.assertRaisesRegex(ConfigError, "at least one operator endpoint"):
            build_operator_connections(None, None, None)
        with self.assertRaisesRegex(ConfigError, "OpenSSH public key"):
            build_operator_connections("not-a-key", None, None)
        with self.assertRaisesRegex(ConfigError, "exact domain"):
            build_operator_connections(None, "*.example.com", "token-value")
        with self.assertRaisesRegex(ConfigError, "KERN_CLOUDFLARE_TUNNEL_TOKEN"):
            build_operator_connections(None, "agent.example.com", None)
        with self.assertRaisesRegex(ConfigError, "single Cloudflare tunnel token"):
            build_operator_connections(None, "agent.example.com", "two tokens")

    def test_default_network_selects_public_default_subnet(self) -> None:
        config = sample_input_config()
        responses = [
            {"Vpcs": [{"VpcId": "vpc-1"}]},
            {
                "Subnets": [
                    {"SubnetId": "subnet-private", "AvailabilityZone": "us-east-1a"},
                    {"SubnetId": "subnet-public", "AvailabilityZone": "us-east-1b"},
                ]
            },
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]}]},
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}]}]},
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertEqual(deploy._default_network(config, {}), ("vpc-1", "subnet-public", "us-east-1b"))

    def test_default_network_rejects_default_vpc_without_public_subnet(self) -> None:
        config = sample_input_config()
        responses = [
            {"Vpcs": [{"VpcId": "vpc-1"}]},
            {"Subnets": [{"SubnetId": "subnet-private"}]},
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]}]},
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            with self.assertRaisesRegex(ConfigError, "internet gateway"):
                deploy._default_network(config, {})

    def test_default_network_can_prefer_existing_volume_availability_zone(self) -> None:
        config = sample_input_config()
        responses = [
            {"Vpcs": [{"VpcId": "vpc-1"}]},
            {
                "Subnets": [
                    {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"},
                    {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1b"},
                ]
            },
            {"RouteTables": [{"Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}]}]},
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertEqual(
                deploy._default_network(config, {}, preferred_availability_zone="us-east-1b"),
                ("vpc-1", "subnet-b", "us-east-1b"),
            )

    def test_security_group_opens_ssh_for_provisioning(self) -> None:
        config = sample_input_config()
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" not in args:
                return {"SecurityGroups": []}
            if args[:2] == ("ec2", "create-security-group"):
                return {"GroupId": "sg-1"}
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" in args:
                return {"SecurityGroups": [{"IpPermissions": [], "IpPermissionsEgress": []}]}
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            self.assertEqual(
                lifecycle_aws._ensure_security_group(config, {}, "vpc-1", ssh_ingress=True, cloudflare_egress=True),
                "sg-1",
            )

        # SSH ingress is opened for provisioning and may be revoked after
        # bootstrap if no persistent SSH endpoint is configured.
        ingress = [call for call in calls if call[:2] == ("ec2", "authorize-security-group-ingress")]
        egress = [call for call in calls if call[:2] == ("ec2", "authorize-security-group-egress")]
        create_group = next(call for call in calls if call[:2] == ("ec2", "create-security-group"))
        create_tags = [call for call in calls if call[:2] == ("ec2", "create-tags")]
        self.assertIn("--tag-specifications", create_group)
        tag_spec = create_group[create_group.index("--tag-specifications") + 1]
        self.assertIn("ResourceType=security-group", tag_spec)
        self.assertIn("Key=kern-host,Value=true", tag_spec)
        self.assertIn("Key=kern-host-agent-name,Value=kern-test", tag_spec)
        self.assertEqual(create_tags, [])
        self.assertEqual(len(ingress), 1)
        self.assertIn('"FromPort": 22', ingress[0][-1])
        # Egress is pinned to HTTP, HTTPS, NTP, and a temporary Cloudflare
        # Tunnel allowance — never all-protocol. The lifecycle CLI revokes 7844
        # after bootstrap when no cloudflare_tunnel endpoint is configured.
        egress_ports = sorted(
            (json.loads(call[-1])[0]["IpProtocol"], json.loads(call[-1])[0]["FromPort"])
            for call in egress
        )
        self.assertEqual(egress_ports, [("tcp", 80), ("tcp", 443), ("tcp", 7844), ("udp", 123), ("udp", 7844)])
        self.assertNotIn('"IpProtocol": "-1"', " ".join(call[-1] for call in egress))

    def test_security_group_can_close_provisioning_ssh_ingress(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-security-groups"):
                return {
                    "SecurityGroups": [
                        {
                            "IpPermissions": [
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 22,
                                    "ToPort": 22,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                },
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 443,
                                    "ToPort": 443,
                                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                },
                            ],
                        }
                    ]
                }
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            deploy._close_security_group_ssh_ingress({}, "sg-1")

        revoke = next(call for call in calls if call[:2] == ("ec2", "revoke-security-group-ingress"))
        revoked_permissions = json.loads(revoke[revoke.index("--ip-permissions") + 1])
        self.assertEqual(len(revoked_permissions), 1)
        self.assertEqual(revoked_permissions[0]["FromPort"], 22)

    def test_ssh_delivery_closes_provisioning_ssh_only_when_endpoints_omit_ssh(self) -> None:
        cloudflare_only = [
            "--operator-cloudflare-hostname",
            "agent.example.com",
            "--admin-password-sha256",
            SAMPLE_ADMIN_PASSWORD_SHA256,
        ]
        ssh_configured = [
            "--operator-ssh-public-key",
            SAMPLE_SSH_PUBLIC_KEY,
            "--admin-password-sha256",
            SAMPLE_ADMIN_PASSWORD_SHA256,
        ]
        for operator_args, expect_revoke in ((cloudflare_only, True), (ssh_configured, False)):
            with self.subTest(expect_revoke=expect_revoke):
                with tempfile.TemporaryDirectory() as tmp:
                    cwd = os.getcwd()
                    os.chdir(tmp)
                    try:
                        with patch.dict(
                            os.environ,
                            {**SAMPLE_AWS_ENV, "KERN_CLOUDFLARE_TUNNEL_TOKEN": "token-value"},
                        ), \
                                patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value=None), \
                                patch("host.cli.lifecycle._find_existing_instances", return_value=[]), \
                                patch("host.cli.lifecycle._existing_storage_roles", return_value=set()), \
                                patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1", "us-east-1a")), \
                                patch("host.cli.lifecycle._generate_deploy_key", side_effect=_fake_deploy_key), \
                                patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")) as launch_instance, \
                                patch(
                                    "host.cli.lifecycle._wait_for_instance",
                                    return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                                ), \
                                patch("host.cli.lifecycle._ensure_storage_volumes", return_value={"admin": "vol-admin", "agent": "vol-agent"}), \
                                patch("host.cli.lifecycle._attach_storage_volumes"), \
                                patch("host.cli.lifecycle._provision_over_ssh"), \
                                patch("host.cli.lifecycle._close_security_group_ssh_ingress") as ssh_ingress, \
                                patch("host.cli.lifecycle_aws._aws", return_value={}), \
                                patch("sys.stderr", _StringOutput()), \
                                patch("sys.stdout", _StringOutput()):
                            self.assertEqual(
                                deploy.main_for_mode(
                                    "deploy",
                                    ["--agent-name", "kern-test", *operator_args],
                                ),
                                0,
                            )
                    finally:
                        os.chdir(cwd)

                # The launch carries the derived final state, with SSH forced
                # open for the deploy key; the only post-bootstrap step is the
                # optional SSH revoke.
                launch_kwargs = launch_instance.call_args.kwargs
                self.assertTrue(launch_kwargs["ssh_ingress"])
                self.assertEqual(launch_kwargs["cloudflare_egress"], expect_revoke)
                if expect_revoke:
                    ssh_ingress.assert_called_once()
                    self.assertEqual(ssh_ingress.call_args.args[1], "sg-1")
                else:
                    ssh_ingress.assert_not_called()

    def test_existing_instance_lookup_requires_kern_owner_tag(self) -> None:
        config = sample_input_config()
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            return {"Reservations": [{"Instances": [{"InstanceId": "i-owned"}]}]}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            self.assertEqual(deploy._find_existing_instances(config, {}), ["i-owned"])

        filters = calls[0][calls[0].index("--filters") + 1:]
        self.assertIn("Name=tag:kern-host-agent-name,Values=kern-test", filters)
        self.assertIn("Name=tag:kern-host,Values=true", filters)

    def test_existing_security_group_without_owner_tag_is_rejected(self) -> None:
        config = sample_input_config()

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" not in args:
                return {"SecurityGroups": [{"GroupId": "sg-1", "Tags": []}]}
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" in args:
                return {"SecurityGroups": [{"IpPermissions": [], "IpPermissionsEgress": []}]}
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            with self.assertRaisesRegex(ConfigError, "not tagged as a Kern resource"):
                lifecycle_aws._ensure_security_group(config, {}, "vpc-1", ssh_ingress=True, cloudflare_egress=True)

    def test_iam_policies_restrict_kern_resource_access(self) -> None:
        policy = json.loads(Path("iam_policy.json").read_text())
        smoke_policy = json.loads(Path("tests/smoke/iam_policy_smoke.json").read_text())
        stage_policy = json.loads(Path("tests/stage/iam_policy_stage.json").read_text())
        for scoped_policy, agent_name in ((smoke_policy, "kern-smoke"), (stage_policy, "kern-stage")):
            policy_without_agent_name = copy.deepcopy(scoped_policy)
            scoped_statements = {statement["Sid"]: statement for statement in policy_without_agent_name["Statement"]}
            self.assertEqual(
                scoped_statements["CreateTaggedKernResources"]["Condition"]["StringEquals"].pop(
                    "aws:RequestTag/kern-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(
                scoped_statements["RunInstancesWithKernSecurityGroups"]["Condition"]["StringEquals"].pop(
                    "aws:ResourceTag/kern-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(
                scoped_statements["TagOnlyDuringKernResourceCreation"]["Condition"]["StringEquals"].pop(
                    "aws:RequestTag/kern-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(
                scoped_statements["ManageOnlyKernResources"]["Condition"]["StringEquals"].pop(
                    "aws:ResourceTag/kern-host-agent-name"
                ),
                agent_name,
            )
            self.assertEqual(policy, policy_without_agent_name)
        self.assertNotIn("aws:RequestedRegion", json.dumps(policy))
        statements = {statement["Sid"]: statement for statement in policy["Statement"]}

        aws_login_statement = statements["AwsLogin"]
        self.assertEqual(
            aws_login_statement["Action"],
            ["signin:AuthorizeOAuth2Access", "signin:CreateOAuth2Token"],
        )
        self.assertEqual(
            aws_login_statement["Resource"],
            "arn:aws:signin:*:*:oauth2/public-client/*",
        )

        discovery_actions = statements["Ec2Discovery"]["Action"]
        self.assertNotIn("ec2:RunInstances", discovery_actions)
        self.assertNotIn("ec2:CreateVolume", discovery_actions)
        self.assertNotIn("ec2:CreateSecurityGroup", discovery_actions)
        self.assertNotIn("ec2:CreateTags", discovery_actions)
        self.assertNotIn("ec2:AuthorizeSecurityGroupIngress", discovery_actions)
        self.assertNotIn("ec2:AuthorizeSecurityGroupEgress", discovery_actions)

        create_statement = statements["CreateTaggedKernResources"]
        create_conditions = create_statement["Condition"]
        self.assertEqual(
            sorted(create_statement["Action"]),
            ["ec2:CreateSecurityGroup", "ec2:CreateVolume", "ec2:RunInstances"],
        )
        self.assertEqual(create_statement["Resource"], "*")
        self.assertEqual(create_conditions["StringEquals"]["aws:RequestTag/kern-host"], "true")
        self.assertNotIn("ec2:InstanceType", create_conditions["StringEquals"])
        self.assertNotIn("aws:RequestTag/kern-host-volume-role", create_conditions["StringEquals"])
        self.assertNotIn("ForAllValues:StringEquals", create_conditions)

        dependency_statement = statements["UseEc2CreateDependencies"]
        self.assertEqual(
            sorted(dependency_statement["Action"]),
            ["ec2:CreateSecurityGroup", "ec2:RunInstances"],
        )
        self.assertEqual(
            sorted(dependency_statement["Resource"]),
            [
                "arn:aws:ec2:*:*:network-interface/*",
                "arn:aws:ec2:*:*:subnet/*",
                "arn:aws:ec2:*:*:vpc/*",
                "arn:aws:ec2:*::image/*",
            ],
        )
        self.assertNotIn("Condition", dependency_statement)

        launch_security_group_statement = statements["RunInstancesWithKernSecurityGroups"]
        self.assertEqual(launch_security_group_statement["Action"], "ec2:RunInstances")
        self.assertEqual(launch_security_group_statement["Resource"], "arn:aws:ec2:*:*:security-group/*")
        self.assertEqual(
            launch_security_group_statement["Condition"]["StringEquals"]["aws:ResourceTag/kern-host"],
            "true",
        )

        tag_statement = statements["TagOnlyDuringKernResourceCreation"]
        self.assertEqual(tag_statement["Action"], "ec2:CreateTags")
        tag_conditions = tag_statement["Condition"]
        self.assertEqual(tag_conditions["StringEquals"]["aws:RequestTag/kern-host"], "true")
        self.assertEqual(
            sorted(tag_conditions["StringEquals"]["ec2:CreateAction"]),
            ["CreateSecurityGroup", "CreateVolume", "RunInstances"],
        )
        self.assertNotIn("ForAllValues:StringEquals", tag_conditions)

        manage_statement = statements["ManageOnlyKernResources"]
        self.assertEqual(
            sorted(manage_statement["Action"]),
            [
                "ec2:AttachVolume",
                "ec2:AuthorizeSecurityGroupEgress",
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:DeleteSecurityGroup",
                "ec2:DeleteVolume",
                "ec2:GetConsoleOutput",
                "ec2:ModifyInstanceAttribute",
                "ec2:RevokeSecurityGroupEgress",
                "ec2:RevokeSecurityGroupIngress",
                "ec2:StartInstances",
                "ec2:StopInstances",
                "ec2:TerminateInstances",
            ],
        )
        self.assertEqual(manage_statement["Resource"], "*")
        self.assertEqual(manage_statement["Condition"]["StringEquals"]["aws:ResourceTag/kern-host"], "true")

        self.assertEqual(
            statements["UbuntuAmiLookup"]["Resource"],
            "arn:aws:ssm:*::parameter/aws/service/canonical/*",
        )

    def test_storage_volumes_are_created_tagged_and_attached(self) -> None:
        config = sample_input_config()
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-volumes"):
                return {"Volumes": []}
            if args[:2] == ("ec2", "create-volume"):
                tag_spec = args[args.index("--tag-specifications") + 1]
                if "Value=admin" in tag_spec:
                    return {"VolumeId": "vol-admin"}
                if "Value=agent" in tag_spec:
                    return {"VolumeId": "vol-agent"}
                raise AssertionError(f"unexpected tag spec: {tag_spec}")
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            created_out: list[str] = []
            volumes = deploy._ensure_storage_volumes(
                config,
                {},
                availability_zone="us-east-1a",
                created_storage_volumes=created_out,
            )
            deploy._attach_storage_volumes({}, instance_id="i-123", volumes=volumes)

        self.assertEqual(volumes, {"admin": "vol-admin", "agent": "vol-agent"})
        self.assertEqual(created_out, ["vol-admin", "vol-agent"])
        create_calls = [call for call in calls if call[:2] == ("ec2", "create-volume")]
        self.assertEqual(len(create_calls), 2)
        self.assertIn("--availability-zone", create_calls[0])
        self.assertIn("us-east-1a", create_calls[0])
        self.assertIn("--encrypted", create_calls[0])
        self.assertEqual(create_calls[0][create_calls[0].index("--size") + 1], "16")
        self.assertEqual(create_calls[1][create_calls[1].index("--size") + 1], "16")
        attach_calls = [call for call in calls if call[:2] == ("ec2", "attach-volume")]
        self.assertEqual(len(attach_calls), 2)
        self.assertIn("/dev/sdf", attach_calls[0])
        self.assertIn("/dev/sdg", attach_calls[1])
        preserve_calls = [call for call in calls if call[:2] == ("ec2", "modify-instance-attribute")]
        self.assertEqual(len(preserve_calls), 2)
        self.assertEqual(preserve_calls[0][preserve_calls[0].index("--instance-id") + 1], "i-123")
        self.assertEqual(preserve_calls[1][preserve_calls[1].index("--instance-id") + 1], "i-123")
        admin_mapping = json.loads(preserve_calls[0][preserve_calls[0].index("--block-device-mappings") + 1])
        agent_mapping = json.loads(preserve_calls[1][preserve_calls[1].index("--block-device-mappings") + 1])
        self.assertEqual(admin_mapping, [{"DeviceName": "/dev/sdf", "Ebs": {"DeleteOnTermination": False}}])
        self.assertEqual(agent_mapping, [{"DeviceName": "/dev/sdg", "Ebs": {"DeleteOnTermination": False}}])

    def test_launch_instance_sets_terminate_on_shutdown(self) -> None:
        config = sample_input_config()
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-security-groups") and "--group-ids" not in args:
                return {"SecurityGroups": []}
            if args[:2] == ("ec2", "create-security-group"):
                return {"GroupId": "sg-1"}
            if args[:2] == ("ec2", "describe-security-groups"):
                return {"SecurityGroups": [{"IpPermissions": [], "IpPermissionsEgress": []}]}
            if args[:2] == ("ssm", "get-parameter"):
                return {"Parameter": {"Value": "ami-123"}}
            if args[:2] == ("ec2", "run-instances"):
                return {"Instances": [{"InstanceId": "i-123"}]}
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
                instance_id, _group = deploy._launch_instance(
                    config,
                    "#!/usr/bin/env bash\n",
                    Path(tmp),
                    {},
                    target_version="0.35.0",
                    network=("vpc-1", "subnet-1", "us-east-1a"),
                    ssh_ingress=True,
                    cloudflare_egress=True,
                )
        self.assertEqual(instance_id, "i-123")
        run = next(call for call in calls if call[:2] == ("ec2", "run-instances"))
        # An OS-initiated shutdown terminates the instance, so a detached
        # provisioning failure can clean up its own instance.
        self.assertIn("--instance-initiated-shutdown-behavior", run)
        self.assertEqual(run[run.index("--instance-initiated-shutdown-behavior") + 1], "terminate")

    def test_storage_volume_lookup_rejects_attached_or_duplicate_state(self) -> None:
        config = sample_input_config()

        with patch(
            "host.cli.lifecycle_aws._aws",
            return_value={"Volumes": [{"VolumeId": "vol-admin", "State": "in-use", "AvailabilityZone": "us-east-1a"}]},
        ):
            with self.assertRaisesRegex(ConfigError, "is in-use"):
                lifecycle_aws._find_available_storage_volume(config, {}, "admin", "us-east-1a")

        with patch(
            "host.cli.lifecycle_aws._aws",
            return_value={
                "Volumes": [
                    {"VolumeId": "vol-admin-a", "State": "available", "AvailabilityZone": "us-east-1a"},
                    {"VolumeId": "vol-admin-b", "State": "available", "AvailabilityZone": "us-east-1a"},
                ]
            },
        ):
            with self.assertRaisesRegex(ConfigError, "multiple Kern admin volumes"):
                lifecycle_aws._find_available_storage_volume(config, {}, "admin", "us-east-1a")

    def test_existing_storage_volumes_are_preserved_before_instance_termination(self) -> None:
        config = sample_input_config()
        calls: list[tuple[str, ...]] = []

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            calls.append(args)
            if args[:2] == ("ec2", "describe-volumes"):
                role = next(arg for arg in args if arg.startswith("Name=tag:kern-host-volume-role,Values="))
                if role.endswith("admin"):
                    return {"Volumes": [{"VolumeId": "vol-admin", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
                return {"Volumes": [{"VolumeId": "vol-agent", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
            if args[:2] == ("ec2", "describe-instances"):
                return {
                    "Reservations": [
                        {
                            "Instances": [
                                {
                                    "InstanceId": "i-old",
                                    "BlockDeviceMappings": [
                                        {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-root"}},
                                        {"DeviceName": "/dev/sdf", "Ebs": {"VolumeId": "vol-admin"}},
                                        {"DeviceName": "/dev/sdg", "Ebs": {"VolumeId": "vol-agent"}},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            return {}

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            deploy._preserve_existing_storage_volumes_on_instance_termination(config, {}, ["i-old"])

        preserve_calls = [call for call in calls if call[:2] == ("ec2", "modify-instance-attribute")]
        self.assertEqual(len(preserve_calls), 2)
        mappings = [
            json.loads(call[call.index("--block-device-mappings") + 1])
            for call in preserve_calls
        ]
        self.assertIn([{"DeviceName": "/dev/sdf", "Ebs": {"DeleteOnTermination": False}}], mappings)
        self.assertIn([{"DeviceName": "/dev/sdg", "Ebs": {"DeleteOnTermination": False}}], mappings)
        self.assertNotIn("/dev/sda1", json.dumps(mappings))

    def test_storage_volume_lookup_can_wait_for_detach_after_replacing_instance(self) -> None:
        config = sample_input_config()
        calls: list[tuple[str, ...]] = []
        describe_count = 0

        def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
            nonlocal describe_count
            calls.append(args)
            if args[:2] == ("ec2", "describe-volumes"):
                describe_count += 1
                state = "in-use" if describe_count == 1 else "available"
                return {"Volumes": [{"VolumeId": "vol-admin", "State": state, "AvailabilityZone": "us-east-1a"}]}
            if args[:3] == ("ec2", "wait", "volume-available"):
                return {}
            raise AssertionError(f"unexpected AWS call: {args}")

        with patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws):
            volume_id = lifecycle_aws._find_available_storage_volume(
                config,
                {},
                "admin",
                "us-east-1a",
                wait_for_detach=True,
            )

        self.assertEqual(volume_id, "vol-admin")
        self.assertIn(("ec2", "wait", "volume-available", "--volume-ids", "vol-admin"), calls)
        self.assertEqual(describe_count, 2)

    def test_existing_storage_volume_az_steers_redeploy_and_detects_split_volumes(self) -> None:
        config = sample_input_config()
        responses = [
            {"Volumes": [{"VolumeId": "vol-admin", "State": "available", "AvailabilityZone": "us-east-1a"}]},
            {"Volumes": [{"VolumeId": "vol-agent", "State": "available", "AvailabilityZone": "us-east-1a"}]},
        ]
        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertEqual(deploy._existing_storage_volume_availability_zone(config, {}), "us-east-1a")

        responses = [
            {"Volumes": [{"VolumeId": "vol-admin", "State": "available", "AvailabilityZone": "us-east-1a"}]},
            {"Volumes": [{"VolumeId": "vol-agent", "State": "available", "AvailabilityZone": "us-east-1b"}]},
        ]
        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            with self.assertRaisesRegex(ConfigError, "split across availability zones"):
                deploy._existing_storage_volume_availability_zone(config, {})

    def test_main_validates_storage_volumes_before_terminating_existing_host(self) -> None:
        calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, dict(SAMPLE_AWS_ENV)), \
                        patch("host.cli.lifecycle._find_existing_instances", side_effect=lambda *_args: calls.append("find_instances") or ["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", side_effect=lambda *_args: calls.append("validate_storage") or "us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._default_network", side_effect=lambda *_args, **_kwargs: calls.append("preflight_network") or ("vpc-1", "subnet-1", "us-east-1a")), \
                        patch("host.cli.lifecycle._terminate_instances", side_effect=lambda *_args: calls.append("terminate")), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=_fake_deploy_key), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")) as launch_instance, \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value={"admin": "vol-admin", "agent": "vol-agent"}), \
                        patch("host.cli.lifecycle._attach_storage_volumes"), \
                        patch("host.cli.lifecycle._provision_over_ssh"), \
                        patch("host.cli.lifecycle._close_security_group_ssh_ingress"), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()):
                    self.assertEqual(deploy.main_for_mode("upgrade", ["--agent-name", "kern-test"]), 0)
            finally:
                os.chdir(cwd)

        self.assertLess(calls.index("validate_storage"), calls.index("terminate"))
        self.assertLess(calls.index("preflight_network"), calls.index("terminate"))
        launch_instance.assert_called_once()
        self.assertEqual(launch_instance.call_args.kwargs["network"], ("vpc-1", "subnet-1", "us-east-1a"))

    def test_main_does_not_terminate_existing_host_when_replacement_network_fails(self) -> None:
        calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, dict(SAMPLE_AWS_ENV)), \
                        patch("host.cli.lifecycle._find_existing_instances", side_effect=lambda *_args: calls.append("find_instances") or ["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", side_effect=lambda *_args: calls.append("validate_storage") or "us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._check_existing_version_hints"), \
                        patch(
                            "host.cli.lifecycle._default_network",
                            side_effect=lambda *_args, **_kwargs: calls.append("preflight_network")
                            or (_ for _ in ()).throw(ConfigError("AWS default VPC has no public subnet in us-east-1a")),
                        ), \
                        patch("host.cli.lifecycle._terminate_instances", side_effect=lambda *_args: calls.append("terminate")), \
                        patch("host.cli.lifecycle._launch_instance", side_effect=AssertionError("_launch_instance should not run")), \
                        patch("sys.stdout", _StringOutput()), \
                        patch("sys.stderr", _StringOutput()):
                    self.assertEqual(deploy.main_for_mode("upgrade", ["--agent-name", "kern-test"]), 2)
            finally:
                os.chdir(cwd)

        self.assertEqual(calls, ["validate_storage", "find_instances", "preflight_network"])

    def test_version_tag_guard_rejects_mode_specific_bootstrap_failures_before_replacement(self) -> None:
        config = sample_input_config()
        cases = [
            (deploy.LifecycleCommand(mode="upgrade", agent_name="kern-test"), "0.6.0", "older than target VERSION"),
            (deploy.LifecycleCommand(mode="recover", agent_name="kern-test"), "0.5.0", "match target VERSION"),
            (
                deploy.LifecycleCommand(mode="reconfigure", agent_name="kern-test"),
                "0.5.0",
                "reconfigure requires preserved state to match",
            ),
            (
                deploy.LifecycleCommand(mode="recover", agent_name="kern-test", allow_upgrade=True),
                "0.7.0",
                "cannot move preserved state backward",
            ),
        ]
        for command, tagged_version, message in cases:
            with self.subTest(command=command, tagged_version=tagged_version):
                response = {
                    "Reservations": [{
                        "Instances": [{
                            "InstanceId": "i-tagged",
                            "Tags": [{"Key": "kern-host-version", "Value": tagged_version}],
                        }]
                    }]
                }
                with patch("host.cli.lifecycle_aws._aws", return_value=response):
                    with self.assertRaisesRegex(ConfigError, message):
                        deploy._check_existing_version_hints(command, config, {}, ["i-tagged"], "0.6.0")

    def test_version_tag_guard_rejects_invalid_tags_before_replacement(self) -> None:
        config = sample_input_config()
        command = deploy.LifecycleCommand(mode="recover", agent_name="kern-test", allow_upgrade=True)
        response = {
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-invalid",
                    "Tags": [{"Key": "kern-host-version", "Value": "not-a-version"}],
                }]
            }]
        }
        with patch("host.cli.lifecycle_aws._aws", return_value=response):
            with self.assertRaisesRegex(ConfigError, "invalid kern-host-version tag"):
                deploy._check_existing_version_hints(command, config, {}, ["i-invalid"], "0.1.0")

    def test_reconfigure_passes_admin_password_hash_and_operator_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, dict(SAMPLE_AWS_ENV)), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value="us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1", "us-east-1a")), \
                        patch("host.cli.lifecycle._terminate_instances"), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=_fake_deploy_key), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")) as launch_instance, \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value={"admin": "vol-admin", "agent": "vol-agent"}), \
                        patch("host.cli.lifecycle._attach_storage_volumes"), \
                        patch("host.cli.lifecycle._provision_over_ssh") as provision, \
                        patch("host.cli.lifecycle._close_security_group_ssh_ingress"), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("builtins.input", side_effect=AssertionError("input should not be called")), \
                        patch("sys.stderr", _StringOutput()), \
                        patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(
                        deploy.main_for_mode(
                            "reconfigure",
                            [
                                "--agent-name",
                                "kern-test",
                                "--operator-ssh-public-key",
                                SAMPLE_SSH_PUBLIC_KEY,
                                "--admin-password-sha256",
                                SAMPLE_ADMIN_PASSWORD_SHA256,
                            ],
                        ),
                        0,
                    )
            finally:
                os.chdir(cwd)

            # The caller's hash and the replacement connections ride in the
            # payload staged through user data; SSH only delivers code.
            provision.assert_called_once()
            user_data = launch_instance.call_args.args[1]
            embedded = next(line for line in user_data.splitlines() if line.startswith("{"))
            payload = json.loads(embedded)
            self.assertEqual(payload["operation"]["mode"], "reconfigure")
            self.assertEqual(payload["runtime_config"]["admin_password_sha256"], SAMPLE_ADMIN_PASSWORD_SHA256)
            self.assertEqual(
                payload["runtime_config"]["operator_connections"],
                [{"mode": "ssh", "ssh_public_key": "ssh-ed25519 AAAATEST operator@example"}],
            )
            result = json.loads(stdout.value)
            self.assertNotIn("admin_password", result)
            self.assertNotIn(SAMPLE_ADMIN_PASSWORD_SHA256, json.dumps(result))
            self.assertEqual(result["operator_connections"], [{"mode": "ssh"}])

    def test_generate_password_prints_matching_password_and_digest(self) -> None:
        import hashlib

        from host.cli import generate_password

        with patch("sys.stdout", _StringOutput()) as stdout:
            self.assertEqual(generate_password.main(), 0)
        lines = stdout.value.splitlines()
        password = next(line for line in lines if line.startswith("password: ")).removeprefix("password: ")
        digest = next(line for line in lines if line.startswith("sha256:")).split()[-1]
        self.assertGreater(len(password), 20)
        self.assertEqual(hashlib.sha256(password.encode()).hexdigest(), digest)
        self.assertIn("--admin-password-sha256", stdout.value)

    def test_deploy_and_reconfigure_reject_the_empty_password_digest(self) -> None:
        import hashlib

        empty_digest = hashlib.sha256(b"").hexdigest()
        for mode in ("deploy", "reconfigure"):
            with self.subTest(mode=mode):
                with patch("sys.stderr", _StringOutput()) as stderr:
                    with self.assertRaises(SystemExit) as raised:
                        deploy._parse_args("deploy", ["--agent-name", "kern-test", "--admin-password-sha256", empty_digest])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("SHA-256 of an empty password", stderr.value)

    def test_deploy_and_reconfigure_require_the_admin_password_hash(self) -> None:
        for mode in ("deploy", "reconfigure"):
            with self.subTest(mode=mode):
                with patch("sys.stderr", _StringOutput()) as stderr:
                    with self.assertRaises(SystemExit) as raised:
                        deploy._parse_args(mode, ["--agent-name", "kern-test"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--admin-password-sha256", stderr.value)

    def test_reconfigure_requires_an_operator_endpoint(self) -> None:
        with (
            patch.dict(os.environ, dict(SAMPLE_AWS_ENV)),
            patch("sys.stderr", _StringOutput()) as stderr,
        ):
            self.assertEqual(
                deploy.main_for_mode(
                    "reconfigure",
                    [
                        "--agent-name",
                        "kern-test",
                        "--admin-password-sha256",
                        SAMPLE_ADMIN_PASSWORD_SHA256,
                    ],
                ),
                2,
            )
        self.assertIn("at least one operator endpoint", stderr.value)

    def test_start_existing_instance_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "stage.json"
            calls: list[tuple[str, ...]] = []
            describe_instance_count = 0

            def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
                nonlocal describe_instance_count
                calls.append(args)
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" not in args:
                    return {"Reservations": [{"Instances": [{"InstanceId": "i-stage"}]}]}
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" in args:
                    describe_instance_count += 1
                    state = "stopped" if describe_instance_count == 1 else "running"
                    return {
                        "Reservations": [
                            {
                                "Instances": [
                                    {
                                        "InstanceId": "i-stage",
                                        "State": {"Name": state},
                                        "PublicDnsName": "stage.example.com",
                                        "PublicIpAddress": "203.0.113.10",
                                    }
                                ]
                            }
                        ]
                    }
                if args[:2] == ("ec2", "describe-volumes"):
                    role = "admin" if "admin" in " ".join(args) else "agent"
                    return {"Volumes": [{"VolumeId": f"vol-{role}", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
                return {}

            with (
                patch.dict(os.environ, dict(SAMPLE_AWS_ENV)),
                patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws),
                patch("host.cli.power._aws", side_effect=fake_aws),
                patch("sys.stdout", _StringOutput()) as stdout,
            ):
                self.assertEqual(
                    power.main_for_power_mode("start", ["--agent-name", "kern-test"]),
                    0,
                )

            self.assertIn(("ec2", "start-instances", "--instance-ids", "i-stage"), calls)
            self.assertIn(("ec2", "wait", "instance-running", "--instance-ids", "i-stage"), calls)
            result = json.loads(stdout.value)
            self.assertEqual(result["agent_name"], "kern-test")
            self.assertEqual(result["instance_id"], "i-stage")
            self.assertEqual(result["state"], "running")
            self.assertEqual(result["public_dns"], "stage.example.com")

    def test_stop_existing_instance_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "stage-stop.json"
            calls: list[tuple[str, ...]] = []
            describe_instance_count = 0

            def fake_aws(_env, *args):  # type: ignore[no-untyped-def]
                nonlocal describe_instance_count
                calls.append(args)
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" not in args:
                    return {"Reservations": [{"Instances": [{"InstanceId": "i-stage"}]}]}
                if args[:2] == ("ec2", "describe-instances") and "--instance-ids" in args:
                    describe_instance_count += 1
                    state = "running" if describe_instance_count == 1 else "stopped"
                    return {
                        "Reservations": [
                            {
                                "Instances": [
                                    {
                                        "InstanceId": "i-stage",
                                        "State": {"Name": state},
                                    }
                                ]
                            }
                        ]
                    }
                if args[:2] == ("ec2", "describe-volumes"):
                    role = "admin" if "admin" in " ".join(args) else "agent"
                    return {"Volumes": [{"VolumeId": f"vol-{role}", "State": "in-use", "AvailabilityZone": "us-east-1a"}]}
                return {}

            with (
                patch.dict(os.environ, dict(SAMPLE_AWS_ENV)),
                patch("host.cli.lifecycle_aws._aws", side_effect=fake_aws),
                patch("host.cli.power._aws", side_effect=fake_aws),
                patch("sys.stdout", _StringOutput()) as stdout,
            ):
                self.assertEqual(
                    power.main_for_power_mode("stop", ["--agent-name", "kern-test"]),
                    0,
                )

            self.assertIn(("ec2", "stop-instances", "--instance-ids", "i-stage"), calls)
            self.assertIn(("ec2", "wait", "instance-stopped", "--instance-ids", "i-stage"), calls)
            result = json.loads(stdout.value)
            self.assertEqual(result["state"], "stopped")

    def test_power_commands_reject_operator_endpoint_flags(self) -> None:
        with patch("sys.stderr", _StringOutput()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                power.main_for_power_mode(
                    "start",
                    ["--agent-name", "kern-test", "--operator-ssh-public-key", SAMPLE_SSH_PUBLIC_KEY],
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.value)

    def test_upgrade_prints_result_json_on_stdout_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, dict(SAMPLE_AWS_ENV)), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=["i-old"]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value="us-east-1a"), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}), \
                        patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1", "us-east-1a")), \
                        patch("host.cli.lifecycle._terminate_instances"), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=_fake_deploy_key), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value={"admin": "vol-admin", "agent": "vol-agent"}), \
                        patch("host.cli.lifecycle._attach_storage_volumes"), \
                        patch("host.cli.lifecycle._provision_over_ssh"), \
                        patch("host.cli.lifecycle._close_security_group_ssh_ingress"), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stderr", _StringOutput()), \
                        patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(deploy.main_for_mode("upgrade", ["--agent-name", "kern-test"]), 0)
            finally:
                os.chdir(cwd)

            # stdout carries exactly the result JSON; no files are written and
            # nothing secret appears.
            upgrade_result = json.loads(stdout.value)
            self.assertEqual(upgrade_result["agent_name"], "kern-test")
            self.assertEqual(upgrade_result["version"], deploy.repo_version())
            self.assertNotIn("admin_password", upgrade_result)
            self.assertNotIn("operator_connections", upgrade_result)
            self.assertEqual(os.listdir(tmp), [])

    def test_failed_deploy_reports_created_data_volumes_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                def fake_ensure_storage(*_args, **kwargs):  # type: ignore[no-untyped-def]
                    kwargs["created_storage_volumes"].extend(["vol-admin", "vol-agent"])
                    return {"admin": "vol-admin", "agent": "vol-agent"}

                with patch.dict(os.environ, dict(SAMPLE_AWS_ENV)), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=[]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value=None), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value=set()), \
                        patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1", "us-east-1a")), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=_fake_deploy_key), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")), \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", side_effect=fake_ensure_storage), \
                        patch("host.cli.lifecycle._attach_storage_volumes"), \
                        patch("host.cli.lifecycle._provision_over_ssh", side_effect=ConfigError("bootstrap failed")), \
                        patch("host.cli.lifecycle._terminate_instances") as terminate, \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stdout", _StringOutput()), \
                        patch("sys.stderr", _StringOutput()) as stderr:
                    self.assertEqual(
                        deploy.main_for_mode(
                            "deploy",
                            [
                                "--agent-name",
                                "kern-test",
                                "--operator-ssh-public-key",
                                SAMPLE_SSH_PUBLIC_KEY,
                                "--admin-password-sha256",
                                SAMPLE_ADMIN_PASSWORD_SHA256,
                            ],
                        ),
                        2,
                    )
            finally:
                os.chdir(cwd)

        # One volume rule, both deliveries: created volumes are never
        # auto-deleted; the retry refuses them until the operator deletes them.
        terminate.assert_called_once()
        self.assertIn("vol-admin, vol-agent", stderr.value)
        self.assertIn("delete the tagged volumes before retrying deploy", stderr.value)

    def test_preflight_deploy_rejects_preserved_resources(self) -> None:
        config = sample_input_config()
        command = deploy.LifecycleCommand(mode="deploy", agent_name="kern-test")
        with self.assertRaisesRegex(ConfigError, "no existing Kern instance"):
            deploy._validate_command_preflight(command, config, ["i-old"], set())
        with self.assertRaisesRegex(ConfigError, "no existing Kern data volumes"):
            deploy._validate_command_preflight(command, config, [], {"admin"})
        with self.assertRaisesRegex(ConfigError, "previous first-time deploy failed"):
            deploy._validate_command_preflight(command, config, [], {"admin", "agent"})

    def test_preflight_reconfigure_requires_existing_instance(self) -> None:
        config = sample_input_config()
        command = deploy.LifecycleCommand(mode="reconfigure", agent_name="kern-test")
        with self.assertRaisesRegex(ConfigError, "reconfigure requires an existing Kern instance"):
            deploy._validate_command_preflight(command, config, [], {"admin", "agent"})
        deploy._validate_command_preflight(command, config, ["i-old"], {"admin", "agent"})

    def test_ssh_user_data_stages_payload_and_deploy_key(self) -> None:
        payload = render._bootstrap_payload(
            sample_input_config(),
            SAMPLE_ADMIN_PASSWORD_SHA256,
            build_operator_connections(SAMPLE_SSH_PUBLIC_KEY, None, None),
            {"admin": "vol-admin", "agent": "vol-agent"},
            mode="deploy",
            target_version="0.35.0",
        )
        user_data = render._render_ssh_user_data(payload, "ssh-ed25519 AAAADEPLOY kern-deploy")

        self.assertLess(len(user_data.encode()), 16_384)
        self.assertIn("useradd --create-home --shell /bin/bash kern-operator", user_data)
        self.assertIn("ssh-ed25519 AAAADEPLOY kern-deploy", user_data)
        self.assertIn("kern-operator ALL=(ALL) NOPASSWD:ALL", user_data)
        self.assertIn("gpasswd -d ubuntu sudo", user_data)
        # Both deliveries stage the same payload through user data; the host
        # receives only the password hash.
        embedded = next(line for line in user_data.splitlines() if line.startswith("{"))
        self.assertEqual(json.loads(embedded), payload)
        self.assertIn(SAMPLE_ADMIN_PASSWORD_SHA256, user_data)
        self.assertNotIn("@PAYLOAD_JSON@", user_data)
        self.assertNotIn("@DEPLOY_PUBLIC_KEY@", user_data)

    def test_bootstrap_from_github_flag_validation(self) -> None:
        base = ["--agent-name", "kern-test", "--admin-password-sha256", SAMPLE_ADMIN_PASSWORD_SHA256]
        parsed = deploy._parse_args("deploy", [*base, "--bootstrap-from-github", "a" * 40])
        self.assertEqual(parsed.github_commit_sha, "a" * 40)
        # Without a value the flag pins the latest main commit.
        parsed = deploy._parse_args("deploy", [*base, "--bootstrap-from-github"])
        self.assertEqual(parsed.github_commit_sha, "")
        parsed = deploy._parse_args("deploy", base)
        self.assertIsNone(parsed.github_commit_sha)
        for argv, message in (
            ([*base, "--bootstrap-from-github", "abc123"], "lowercase hex commit sha"),
            ([*base, "--bootstrap-from-github", "A" * 40], "lowercase hex commit sha"),
            (["--agent-name", "kern-test", "--admin-password-sha256", "zz"], "hex SHA-256 digest"),
        ):
            with self.subTest(argv=argv):
                with patch("sys.stderr", _StringOutput()) as stderr:
                    with self.assertRaises(SystemExit) as raised:
                        deploy._parse_args("deploy", argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, stderr.value)

    def test_render_github_user_data_embeds_payload_and_pin(self) -> None:
        config = sample_input_config()
        payload = render._bootstrap_payload(
            config,
            SAMPLE_ADMIN_PASSWORD_SHA256,
            build_operator_connections(SAMPLE_SSH_PUBLIC_KEY, None, None),
            {"admin": "vol-admin", "agent": "vol-agent"},
            mode="deploy",
            target_version="0.35.0",
        )
        user_data = render._render_github_user_data(payload, "b" * 40)

        # Payload embeds as one line so it can never collide with the heredoc
        # delimiter, and the whole script stays far below the 16 KiB user-data cap.
        self.assertLess(len(user_data.encode()), 16_384)
        embedded = next(line for line in user_data.splitlines() if line.startswith("{"))
        self.assertEqual(json.loads(embedded), payload)
        self.assertIn("https://github.com/infiloop2/kern.git", user_data)
        self.assertIn("git fetch -q --depth 1 origin '" + "b" * 40 + "'", user_data)
        self.assertIn("python3 -m host.bootstrap.self_provision", user_data)
        # The CLI preflight already proved the commit readable, so host-side
        # fetch failures are transient; both network steps retry for an
        # extended window instead of bricking the instance.
        self.assertEqual(user_data.count("for attempt in $(seq 1 60); do"), 2)
        self.assertIn("sleep 30", user_data)
        # The first-boot apt timers are stopped before the git install (they
        # hold the dpkg lock for unbounded archive downloads otherwise); the
        # same-version bootstrap restarts them after its own apt work.
        self.assertIn("systemctl stop apt-daily.timer apt-daily-upgrade.timer", user_data)
        self.assertNotIn("systemctl start apt-daily.timer", user_data)
        self.assertLess(
            user_data.index("systemctl stop apt-daily.timer"),
            user_data.index("apt-get -q -o DPkg::Lock::Timeout=300"),
        )
        self.assertIn("useradd --create-home --shell /bin/bash kern-operator", user_data)
        self.assertIn("gpasswd -d ubuntu sudo", user_data)
        # The host receives only the password hash, and no deploy key exists
        # in this delivery.
        self.assertIn(SAMPLE_ADMIN_PASSWORD_SHA256, user_data)
        self.assertNotIn("authorized_keys2", user_data)
        # A provisioning failure shuts the instance down, which terminates it
        # because instances launch with terminate-on-shutdown behavior.
        self.assertIn("trap on_exit EXIT", user_data)
        self.assertIn("shutdown -h now", user_data)
        self.assertNotIn("@PAYLOAD_JSON@", user_data)
        self.assertNotIn("@GITHUB_REPOSITORY@", user_data)
        self.assertNotIn("@COMMIT_SHA@", user_data)

    def test_github_deploy_launches_with_payload_and_no_ssh_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, dict(SAMPLE_AWS_ENV)), \
                        patch("host.cli.lifecycle._find_existing_instances", return_value=[]), \
                        patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value=None), \
                        patch("host.cli.lifecycle._existing_storage_roles", return_value=set()), \
                        patch("host.cli.lifecycle._resolve_github_pin", return_value=("c" * 40, "0.35.0")), \
                        patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1", "us-east-1a")), \
                        patch("host.cli.lifecycle._generate_deploy_key", side_effect=AssertionError("no deploy key in the GitHub delivery")), \
                        patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")) as launch_instance, \
                        patch(
                            "host.cli.lifecycle._wait_for_instance",
                            return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                        ), \
                        patch("host.cli.lifecycle._ensure_storage_volumes", return_value={"admin": "vol-admin", "agent": "vol-agent"}), \
                        patch("host.cli.lifecycle._attach_storage_volumes") as attach_volumes, \
                        patch("host.cli.lifecycle._provision_over_ssh", side_effect=AssertionError("no SSH in the GitHub delivery")), \
                        patch("host.cli.lifecycle._close_security_group_ssh_ingress", side_effect=AssertionError("access is final at launch")), \
                        patch("host.cli.lifecycle_aws._aws", return_value={}), \
                        patch("sys.stderr", _StringOutput()), \
                        patch("sys.stdout", _StringOutput()) as stdout:
                    self.assertEqual(
                        deploy.main_for_mode(
                            "deploy",
                            [
                                "--agent-name",
                                "kern-test",
                                "--operator-ssh-public-key",
                                SAMPLE_SSH_PUBLIC_KEY,
                                "--admin-password-sha256",
                                SAMPLE_ADMIN_PASSWORD_SHA256,
                                "--bootstrap-from-github",
                                "c" * 40,
                            ],
                        ),
                        0,
                    )
            finally:
                os.chdir(cwd)

            attach_volumes.assert_called_once()
            launch_kwargs = launch_instance.call_args.kwargs
            # Only an ssh operator endpoint is configured: SSH ingress is
            # final at launch and the Cloudflare connector egress stays closed.
            self.assertTrue(launch_kwargs["ssh_ingress"])
            self.assertFalse(launch_kwargs["cloudflare_egress"])
            user_data = launch_instance.call_args.args[1]
            embedded = next(line for line in user_data.splitlines() if line.startswith("{"))
            payload = json.loads(embedded)
            self.assertEqual(payload["operation"]["mode"], "deploy")
            self.assertEqual(payload["storage_volumes"], {"admin": "vol-admin", "agent": "vol-agent"})
            self.assertEqual(payload["runtime_config"]["admin_password_sha256"], SAMPLE_ADMIN_PASSWORD_SHA256)
            result = json.loads(stdout.value)
            self.assertEqual(result["github_source"], "infiloop2/kern@" + "c" * 40)
            self.assertNotIn("admin_password", result)

    def test_github_upgrade_reapplies_previous_security_group_state(self) -> None:
        for captured, expected in (
            ((False, True), (False, True)),
            ((True, False), (True, False)),
            (None, (False, True)),  # missing group: SSH fails closed, connector stays open
        ):
            with self.subTest(captured=captured):
                with tempfile.TemporaryDirectory() as tmp:
                    cwd = os.getcwd()
                    os.chdir(tmp)
                    try:
                        with contextlib.ExitStack() as stack:
                            stack.enter_context(patch.dict(os.environ, dict(SAMPLE_AWS_ENV)))
                            stack.enter_context(patch("host.cli.lifecycle._find_existing_instances", return_value=["i-old"]))
                            stack.enter_context(patch("host.cli.lifecycle._existing_storage_volume_availability_zone", return_value="us-east-1a"))
                            stack.enter_context(patch("host.cli.lifecycle._existing_storage_roles", return_value={"admin", "agent"}))
                            stack.enter_context(patch("host.cli.lifecycle._resolve_github_pin", return_value=("d" * 40, "0.35.0")))
                            stack.enter_context(patch("host.cli.lifecycle._default_network", return_value=("vpc-1", "subnet-1", "us-east-1a")))
                            stack.enter_context(patch("host.cli.lifecycle._security_group_access_state", return_value=captured))
                            stack.enter_context(patch("host.cli.lifecycle._terminate_instances"))
                            launch_instance = stack.enter_context(patch("host.cli.lifecycle._launch_instance", return_value=("i-123", "sg-1")))
                            stack.enter_context(
                                patch(
                                    "host.cli.lifecycle._wait_for_instance",
                                    return_value={"PublicDnsName": "ec2.example", "Placement": {"AvailabilityZone": "us-east-1a"}},
                                )
                            )
                            stack.enter_context(patch("host.cli.lifecycle._ensure_storage_volumes", return_value={"admin": "vol-admin", "agent": "vol-agent"}))
                            stack.enter_context(patch("host.cli.lifecycle._attach_storage_volumes"))
                            stack.enter_context(patch("host.cli.lifecycle._provision_over_ssh", side_effect=AssertionError("no SSH in the GitHub delivery")))
                            stack.enter_context(patch("host.cli.lifecycle._close_security_group_ssh_ingress", side_effect=AssertionError("access is final at launch")))
                            stack.enter_context(patch("host.cli.lifecycle_aws._aws", return_value={}))
                            stack.enter_context(patch("sys.stdout", _StringOutput()))
                            self.assertEqual(
                                deploy.main_for_mode(
                                    "upgrade",
                                    ["--agent-name", "kern-test", "--bootstrap-from-github", "d" * 40],
                                ),
                                0,
                            )
                    finally:
                        os.chdir(cwd)

                launch_kwargs = launch_instance.call_args.kwargs
                self.assertEqual(launch_kwargs["ssh_ingress"], expected[0])
                self.assertEqual(launch_kwargs["cloudflare_egress"], expected[1])

    def test_resolve_github_pin_confirms_the_fetched_version(self) -> None:
        class _FakeResponse:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return self._body

        sha = "e" * 40

        # Pinned sha: the fetched version is shown and confirmed.
        with patch("host.cli.lifecycle.urllib.request.urlopen", return_value=_FakeResponse(b"0.36.0\n")) as urlopen, \
                patch("host.cli.lifecycle.repo_version", return_value="0.36.0"), \
                patch("builtins.input", return_value="y"), \
                patch("sys.stderr", _StringOutput()) as stderr:
            self.assertEqual(deploy._resolve_github_pin(sha), (sha, "0.36.0"))
        self.assertIn("raw.githubusercontent.com/infiloop2/kern/" + sha, urlopen.call_args.args[0])
        self.assertIn("Proceed with Kern 0.36.0?", stderr.value)

        # No sha: the latest main commit is resolved first, then confirmed.
        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            del timeout
            url = request if isinstance(request, str) else request.full_url
            if url.startswith("https://api.github.com/"):
                self.assertIn("/repos/infiloop2/kern/commits/main", url)
                return _FakeResponse(json.dumps({"sha": sha}).encode())
            self.assertIn("raw.githubusercontent.com/infiloop2/kern/" + sha, url)
            return _FakeResponse(b"0.36.0\n")

        with patch("host.cli.lifecycle.urllib.request.urlopen", side_effect=fake_urlopen), \
                patch("host.cli.lifecycle.repo_version", return_value="0.36.0"), \
                patch("builtins.input", return_value="y"), \
                patch("sys.stderr", _StringOutput()):
            self.assertEqual(deploy._resolve_github_pin(""), (sha, "0.36.0"))

        # Decline aborts before anything is touched.
        with patch("host.cli.lifecycle.urllib.request.urlopen", return_value=_FakeResponse(b"0.36.0\n")), \
                patch("host.cli.lifecycle.repo_version", return_value="0.36.0"), \
                patch("builtins.input", return_value="n"), \
                patch("sys.stderr", _StringOutput()):
            with self.assertRaisesRegex(ConfigError, "aborted"):
                deploy._resolve_github_pin(sha)

        # No terminal points at piping the confirmation.
        with patch("host.cli.lifecycle.urllib.request.urlopen", return_value=_FakeResponse(b"0.36.0\n")), \
                patch("host.cli.lifecycle.repo_version", return_value="0.36.0"), \
                patch("builtins.input", side_effect=EOFError()), \
                patch("sys.stderr", _StringOutput()):
            with self.assertRaisesRegex(ConfigError, "pipe 'y' into stdin"):
                deploy._resolve_github_pin(sha)

        # A pin whose VERSION differs from this CLI's fails before anything
        # is touched: the user data rendered here and the bootstrap fetched
        # on the instance must come from one version. Deploying older code
        # means running that commit's own CLI.
        with patch("host.cli.lifecycle.urllib.request.urlopen", return_value=_FakeResponse(b"0.34.0\n")), \
                patch("host.cli.lifecycle.repo_version", return_value="0.36.0"):
            with self.assertRaisesRegex(ConfigError, "deploy with its CLI"):
                deploy._resolve_github_pin(sha)

        # GitHub failures and garbage content fail closed.
        with patch("host.cli.lifecycle.urllib.request.urlopen", side_effect=OSError("no network")):
            with self.assertRaisesRegex(ConfigError, "could not read the pinned commit's VERSION"):
                deploy._resolve_github_pin(sha)
        with patch("host.cli.lifecycle.urllib.request.urlopen", side_effect=OSError("no network")):
            with self.assertRaisesRegex(ConfigError, "could not read the latest main commit"):
                deploy._resolve_github_pin("")
        with patch("host.cli.lifecycle.urllib.request.urlopen", return_value=_FakeResponse(b"<html>404</html>")):
            with self.assertRaisesRegex(ConfigError, "invalid VERSION"):
                deploy._resolve_github_pin(sha)

    def test_security_group_access_state_reads_converged_rules(self) -> None:
        config = sample_input_config()

        with patch("host.cli.lifecycle_aws._aws", return_value={"SecurityGroups": []}):
            self.assertIsNone(deploy._security_group_access_state(config, {}, "vpc-1"))

        group = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-1",
                    "IpPermissions": [
                        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    ],
                    "IpPermissionsEgress": [
                        {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    ],
                }
            ]
        }
        with patch("host.cli.lifecycle_aws._aws", return_value=group):
            self.assertEqual(deploy._security_group_access_state(config, {}, "vpc-1"), (True, False))

    def test_self_provision_enforces_version_pin_and_runs_bootstrap(self) -> None:
        from host.bootstrap import self_provision

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            checkout = tmp_path / "checkout"
            checkout.mkdir()
            (checkout / "VERSION").write_text("0.35.0\n")
            payload_path = tmp_path / "payload.json"
            payload_path.write_text(json.dumps({"operation": {"target_version": "0.35.0"}}))
            bootstrap_path = tmp_path / "bootstrap.sh"
            archive_path = tmp_path / "code.tar.gz"

            with patch.object(self_provision, "BOOTSTRAP_PATH", bootstrap_path), \
                    patch.object(self_provision, "CODE_ARCHIVE_PATH", archive_path), \
                    patch("host.bootstrap.self_provision._render_bootstrap", return_value="#!/bin/bash\n") as render_bootstrap, \
                    patch("host.bootstrap.self_provision._write_runtime_code_archive") as write_archive, \
                    patch("host.bootstrap.self_provision.subprocess.run") as run, \
                    patch("sys.stdout", _StringOutput()):
                run.return_value.returncode = 0
                self.assertEqual(
                    self_provision.main(["--payload", str(payload_path), "--checkout", str(checkout)]),
                    0,
                )
            render_bootstrap.assert_called_once()
            write_archive.assert_called_once_with(archive_path)
            run.assert_called_once_with(["bash", str(bootstrap_path)])
            self.assertEqual(bootstrap_path.read_text(), "#!/bin/bash\n")
            self.assertFalse(checkout.exists())  # removed after success

            # A pre-existing archive (the operator-owned file scp'd on the SSH
            # delivery) is removed before the fresh archive is written, so the
            # root rebuild is not blocked by fs.protected_regular in /tmp.
            checkout.mkdir()
            (checkout / "VERSION").write_text("0.35.0\n")
            archive_path.write_text("stale scp'd archive")
            existed_at_write: list[bool] = []
            with patch.object(self_provision, "BOOTSTRAP_PATH", bootstrap_path), \
                    patch.object(self_provision, "CODE_ARCHIVE_PATH", archive_path), \
                    patch("host.bootstrap.self_provision._render_bootstrap", return_value="#!/bin/bash\n"), \
                    patch(
                        "host.bootstrap.self_provision._write_runtime_code_archive",
                        side_effect=lambda path: existed_at_write.append(path.exists()),
                    ), \
                    patch("host.bootstrap.self_provision.subprocess.run") as run, \
                    patch("sys.stdout", _StringOutput()):
                run.return_value.returncode = 0
                self.assertEqual(
                    self_provision.main(["--payload", str(payload_path), "--checkout", str(checkout)]),
                    0,
                )
            self.assertEqual(existed_at_write, [False])

            # A bootstrap failure propagates as exit 1 and keeps the checkout
            # for diagnosis.
            checkout.mkdir()
            (checkout / "VERSION").write_text("0.35.0\n")
            with patch.object(self_provision, "BOOTSTRAP_PATH", bootstrap_path), \
                    patch.object(self_provision, "CODE_ARCHIVE_PATH", archive_path), \
                    patch("host.bootstrap.self_provision._render_bootstrap", return_value="#!/bin/bash\n"), \
                    patch("host.bootstrap.self_provision._write_runtime_code_archive"), \
                    patch("host.bootstrap.self_provision.subprocess.run") as run, \
                    patch("sys.stderr", _StringOutput()):
                run.return_value.returncode = 3
                self.assertEqual(
                    self_provision.main(["--payload", str(payload_path), "--checkout", str(checkout)]),
                    1,
                )
            self.assertTrue(checkout.exists())

            # A version-pin mismatch fails closed before rendering anything.
            (checkout / "VERSION").write_text("0.34.0\n")
            with patch("host.bootstrap.self_provision._render_bootstrap", side_effect=AssertionError("must not render")), \
                    patch("sys.stderr", _StringOutput()) as stderr:
                self.assertEqual(
                    self_provision.main(["--payload", str(payload_path), "--checkout", str(checkout)]),
                    2,
                )
            self.assertIn("the commit you pin must be the code", stderr.value)

    def test_rendered_bootstrap_contains_privilege_boundary(self) -> None:
        bootstrap = render._render_bootstrap()
        self.assertIn("KERN_WORKSPACE_UID=47750", bootstrap)
        self.assertNotIn("migrate_legacy_agent_workspace_identity", bootstrap)
        self.assertNotIn("retire_legacy_app_platform_identities", bootstrap)
        postgres_setup = bootstrap.split("setup_postgres() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertLess(
            postgres_setup.index('CREATE ROLE "kern-workspace" LOGIN;'),
            postgres_setup.index("migrate_legacy_app_identities"),
        )
        self.assertIn("REASSIGN OWNED BY", bootstrap)
        for retired_schema in (
            "app_mission_pursuit",
            "app_alpha_seeker",
            "app_social_marketer",
            "app_virality_machine",
            "app_software_builder",
        ):
            self.assertIn(
                f"DROP SCHEMA IF EXISTS {retired_schema} CASCADE;",
                bootstrap,
            )
        self.assertIn('ensure_user kern-workspace "$KERN_WORKSPACE_UID"', bootstrap)
        self.assertIn("kern-workspace.service", bootstrap)
        self.assertIn("User=kern-workspace", bootstrap)
        self.assertIn("Slice=kern_workspace.slice", bootstrap)
        self.assertIn("adopt_workspace_migration_history", bootstrap)
        self.assertIn(
            "to_regclass('public.workspace_migrations') IS NULL",
            bootstrap,
        )
        self.assertIn("FROM public.workspace_migrations AS old", bootstrap)
        self.assertIn("INSERT INTO public.schema_migrations", bootstrap)
        self.assertNotIn("grant_workspace_runtime_access", bootstrap)
        self.assertNotIn("host.runtime.deploy.workspace_migrate", bootstrap)
        self.assertNotIn("CREATE SCHEMA IF NOT EXISTS app_agent_chat", postgres_setup)
        self.assertNotIn(
            "CREATE SCHEMA IF NOT EXISTS app_personal_web_app_builder",
            postgres_setup,
        )
        self.assertIn('oif lo tcp dport $WORKSPACE_PORT meta skuid "kern-admin" accept', bootstrap)
        self.assertIn("oif lo tcp dport $WORKSPACE_PORT drop", bootstrap)
        self.assertIn('oif lo meta skuid "kern-workspace" drop', bootstrap)
        self.assertNotIn(
            "cat > /etc/systemd/system/kern-app-agent_chat.service",
            bootstrap,
        )
        self.assertNotIn("host.runtime.deploy.app_migrate", bootstrap)
        self.assertNotIn("KERN_APP_AGENT_CHAT_UID", bootstrap)
        self.assertNotIn("kern-agent-workspace", bootstrap)
        self.assertIn("ExecStart=/usr/bin/python3 -m host.runtime.workspace.service", bootstrap)
        self.assertIn("RuntimeDirectory=kern-workspace", bootstrap)
        start_services = bootstrap.split("start_services() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("systemctl enable kern-workspace.service", start_services)
        self.assertIn("systemctl start kern-workspace.service", start_services)
        self.assertIn("RuntimeDirectory=kern-admin-api", bootstrap)
        self.assertIn("LimitNOFILE=8192", bootstrap)
        self.assertIn('oif lo tcp dport 8000-8015 meta skuid "kern-agent" accept', bootstrap)
        self.assertIn('oif lo tcp dport 8000-8015 meta skuid "kern-operator" accept', bootstrap)
        self.assertIn("oif lo tcp dport 8000-8015 drop", bootstrap)
        self.assertIn("tcp dport 22 accept", bootstrap)
        self.assertIn("python3 -m host.runtime.deploy.write_config", bootstrap)
        self.assertNotIn("/var/lib/kern-host", bootstrap)





    def test_rendered_bootstrap_runs_phases_and_verification(self) -> None:
        bootstrap = render._render_bootstrap()

        # The pinned core service accounts render from host.constants, the
        # same table host.bootstrap.verify_deploy checks on the host.
        from host.constants import SERVICE_ACCOUNTS

        for name, uid in SERVICE_ACCOUNTS.items():
            prefix = name.upper().replace("-", "_")
            self.assertIn(f"{prefix}_UID={uid}", bootstrap)
            self.assertIn(f"{prefix}_GID={uid}", bootstrap)
        # main() runs the phases in order and verification sits after the
        # services start and before staged secrets are dropped.
        self.assertIn("python3 -m host.bootstrap.verify_deploy --cloudflare", bootstrap)
        self.assertLess(
            bootstrap.index("\n  start_services\n"), bootstrap.index("\n  verify_deployment\n")
        )
        self.assertLess(
            bootstrap.index("\n  verify_deployment\n"), bootstrap.index("\n  finalize_deploy\n")
        )
        # The sudoers drop-in is validated at write time, not at first use.
        self.assertIn("visudo -c -q -f /etc/sudoers.d/kern-host", bootstrap)
        self.assertTrue(bootstrap.rstrip().endswith("\nmain"))

    def test_rendered_bootstrap_provisions_admin_state_postgres(self) -> None:
        bootstrap = render._render_bootstrap()

        # The Debian default cluster (root volume) is disabled before the
        # server package installs; the real data directory lives on the
        # durable admin volume, versioned by Postgres major.
        self.assertIn("create_main_cluster = false", bootstrap)
        self.assertIn('apt_get install -y "postgresql-${PG_MAJOR}"', bootstrap)
        self.assertIn("PG_MAJOR=14", bootstrap)
        self.assertLess(
            bootstrap.index("create_main_cluster = false"),
            bootstrap.index('apt_get install -y "postgresql-${PG_MAJOR}"'),
        )
        self.assertIn('runuser -u postgres -- "$PG_BIN/initdb" -D "$PGDATA_DIR"', bootstrap)
        # Unix-socket only, peer auth: no TCP listener, admin and superuser
        # roles only, and an explicit reject for everyone else (the agent user
        # has no role and no pg_hba rule that admits it).
        self.assertIn("listen_addresses = ''", bootstrap)
        # The core DB clients plus the workspace service fit below the server
        # cap with explicit room for operator, superuser, and deploy sessions.
        self.assertIn("max_connections = 300", bootstrap)
        self.assertEqual(db.MAX_ACTIVE_CONNECTIONS, 14)
        # 34 slots stay reserved for operator psql, the superuser reserve, and
        # deploy work; the five managed database clients remain bounded.
        active_session_budget = 5 * db.MAX_ACTIVE_CONNECTIONS
        self.assertLessEqual(active_session_budget, 300 - 34)
        self.assertIn("local  kern_admin  kern-admin  peer", bootstrap)
        self.assertIn("local  kern_admin  kern-agent-network  peer", bootstrap)
        self.assertIn("local  kern_admin  kern-workspace  peer", bootstrap)
        self.assertNotIn("local  kern_admin  kern-agent-workspace  peer", bootstrap)
        self.assertIn("local  all               postgres          peer", bootstrap)
        self.assertIn("local  all               all               reject", bootstrap)
        self.assertIn('CREATE ROLE "kern-admin" LOGIN;', bootstrap)
        self.assertIn("createdb --owner=kern-admin kern_admin", bootstrap)
        self.assertIn("REVOKE ALL ON DATABASE kern_admin FROM PUBLIC;", bootstrap)
        # The PUBLIC revoke strips the proxy role's inherited CONNECT; without
        # the explicit grant the fail-closed proxy loses its event log and
        # fails every agent request.
        self.assertIn('GRANT CONNECT ON DATABASE kern_admin TO \\"kern-proxy\\";', bootstrap)
        # The tools service's scoped role: bootstrap provisions the role and its
        # database CONNECT before migrations run; the table grants live in the
        # schema migration (0007), the same pattern as the proxy role's grants.
        self.assertIn('GRANT CONNECT ON DATABASE kern_admin TO \\"kern-tools\\";', bootstrap)
        self.assertIn(
            'GRANT CONNECT ON DATABASE kern_admin TO \\"kern-agent-network\\";',
            bootstrap,
        )
        self.assertNotIn('GRANT SELECT ON enabled_tools', bootstrap)
        # Thread-scope attribution needs no agent-workspace database identity.
        self.assertNotIn('CREATE ROLE "kern-agent-workspace" LOGIN;', bootstrap)
        self.assertNotIn('GRANT CONNECT ON DATABASE kern_admin TO \\"kern-agent-workspace\\";', bootstrap)
        migration = (Path(__file__).resolve().parents[1] / "host" / "migrations" / "0001_baseline.sql").read_text()
        # Read-only on enablement and config (operator-written by the admin
        # API). The genesis baseline grants exactly SELECT: there are no broader
        # grants from an earlier iteration to revoke first on a fresh install.
        self.assertIn('GRANT SELECT ON enabled_tools, tool_config TO "kern-tools";', migration)
        # Read/write on the credentials, approvals, and events it mutates (plus
        # their serial sequences), read on secret_keys to decrypt
        # config/credentials -- nothing else.
        self.assertIn(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON tool_credentials, tool_approvals, '
            'tool_events TO "kern-tools";',
            migration,
        )
        self.assertIn('GRANT USAGE ON SEQUENCE tool_approvals_number_seq, tool_events_seq_seq TO "kern-tools";', migration)
        self.assertIn('GRANT SELECT ON secret_keys TO "kern-tools";', migration)
        # Network-introspection tables reach the agent-network role. All grants
        # now live in the collapsed 0001_baseline.sql, so the former per-file
        # "network migration does not mention kern-tools" isolation check no
        # longer applies (the tool grants legitimately appear in the baseline).
        self.assertIn('TO "kern-agent-network";', migration)
        # Bedrock schema and grant.
        self.assertIn("CREATE TABLE bedrock_credentials (", migration)
        self.assertIn("region TEXT NOT NULL CHECK", migration)
        self.assertIn('GRANT SELECT ON bedrock_credentials TO "kern-proxy";', migration)
        self.assertNotIn("bedrock_settings", migration)
        self.assertNotIn("proxy_bedrock_credentials", migration)
        self.assertNotIn("harness_bedrock_settings", migration)
        # PG14 leaves the public schema creatable by PUBLIC; only the
        # schema-owning admin role may create objects.
        self.assertIn("REVOKE CREATE ON SCHEMA public FROM PUBLIC;", bootstrap)
        self.assertIn('GRANT CREATE ON SCHEMA public TO \\"kern-admin\\";', bootstrap)
        # The database runs under its own unit and the admin API waits for it.
        self.assertIn("/etc/systemd/system/kern-postgres.service", bootstrap)
        self.assertIn("systemctl enable --now kern-postgres.service", bootstrap)
        self.assertIn(
            "After=network-online.target kern-network-proxy.service kern-postgres.service "
            "kern-tools.service kern-agent-network.service",
            bootstrap,
        )
        # Schema migrations and config seeding run as kern-admin, after
        # the cluster is up and before the admin API starts.
        migrate_up = "python3 -m host.runtime.deploy.migrate up"
        self.assertIn("runuser -u kern-admin -- env PYTHONPATH=/opt/kern-host " + migrate_up, bootstrap)
        self.assertIn("python3 -m host.runtime.deploy.write_config", bootstrap)
        migration_sequence = bootstrap.split(
            "migrate_admin_state_and_write_config() {", 1
        )[1].split("\n}", 1)[0]
        self.assertLess(
            migration_sequence.index("migrate up --to 13"),
            migration_sequence.index("adopt_workspace_migration_history"),
        )
        self.assertLess(
            migration_sequence.index("adopt_workspace_migration_history"),
            migration_sequence.rindex(migrate_up),
        )
        self.assertLess(
            bootstrap.index("systemctl enable --now kern-postgres.service"),
            bootstrap.index(migrate_up),
        )
        self.assertLess(
            bootstrap.index(migrate_up),
            bootstrap.index("python3 -m host.runtime.deploy.write_config"),
        )
        self.assertLess(
            bootstrap.index("python3 -m host.runtime.deploy.write_config"),
            bootstrap.index("systemctl enable --now kern-admin-api.service"),
        )
        self.assertLess(
            bootstrap.index("systemctl enable kern-workspace.service"),
            bootstrap.index("systemctl start kern-workspace.service"),
        )
        self.assertLess(
            bootstrap.index("systemctl start kern-workspace.service"),
            bootstrap.index("rm -f /tmp/kern_payload.json /tmp/kern_effective_config.json"),
        )
        self.assertLess(
            bootstrap.index("rm -f /tmp/kern_payload.json /tmp/kern_effective_config.json"),
            bootstrap.index("KERN_TARGET_VERSION"),
        )
        # No database driver anywhere: the runtime speaks the wire protocol
        # itself (host/runtime/core/pgclient.py).
        self.assertNotIn("psycopg2", bootstrap)
        # Root rewrites the managed database config inside the postgres-owned
        # data directory; those slots (and every data-dir path component) are
        # sanitized against planted symlinks first.
        self.assertIn('pgdata / "postgresql.conf",', bootstrap)
        self.assertIn('pgdata / "pg_hba.conf",', bootstrap)
        self.assertIn('pgdata = admin_mount / "postgres" / os.environ["PG_MAJOR"] / "main"', bootstrap)
        self.assertLess(
            bootstrap.index('pgdata / "postgresql.conf"'),
            bootstrap.index('cat > "$PGDATA_DIR/postgresql.conf"'),
        )

    def test_bootstrap_renders_shared_port_constants(self) -> None:
        from host.constants import PROXY_PORT

        bootstrap = render._render_bootstrap()
        # No placeholders left unrendered, and the rendered ports are the shared
        # constants — so a port change in one place cannot silently drift.
        self.assertNotIn("@PROXY_PORT@", bootstrap)
        self.assertNotIn("@ADMIN_PORT@", bootstrap)
        self.assertIn(f"PROXY_PORT={PROXY_PORT}", bootstrap)
        self.assertIn(f'oif lo tcp dport {PROXY_PORT} meta skuid "kern-agent" accept', bootstrap)
        helper = (Path("host/bootstrap/helpers/run-codex-app-server.sh").read_text()).replace(
            "@PROXY_PORT@", str(PROXY_PORT)
        )
        self.assertIn(f"HTTPS_PROXY=http://127.0.0.1:{PROXY_PORT}", helper)

    def test_agent_launchers_expose_the_proxy_ca_to_python_package_clients(self) -> None:
        for name in (
            "run-codex-app-server",
            "run-claude-code",
            "run-hermes",
            "run-agent-script",
        ):
            with self.subTest(name=name):
                launcher = Path(f"host/bootstrap/helpers/{name}.sh").read_text()
                self.assertIn(
                    "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
                    launcher,
                )
                self.assertIn(
                    "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt",
                    launcher,
                )

    def test_rendered_helper_scripts_have_valid_shell_syntax(self) -> None:
        for name in (
            "run-codex-app-server",
            "read-codex-account-id",
            "run-claude-code",
            "read-claude-account",
            "clear-agent-auth",
            "read-agent-file",
            "upload-agent-file",
            "reboot-host",
            "check-for-upgrade",
            "run-hermes",
            "run-agent-script",
        ):
            script = (Path(f"host/bootstrap/helpers/{name}.sh").read_text()).replace("@PROXY_PORT@", "7445")
            with tempfile.NamedTemporaryFile("w", delete=False) as handle:
                handle.write(script)
                script_path = handle.name
            self.addCleanup(lambda path=script_path: Path(path).unlink(missing_ok=True))
            subprocess.run(["bash", "-n", script_path], check=True)

    def test_run_hermes_uses_bootstrap_config_and_passes_the_runtime_region(self) -> None:
        launcher = Path("host/bootstrap/helpers/run-hermes.sh").read_text()
        self.assertIn('AWS_REGION="${region}"', launcher)
        self.assertNotIn("config.yaml", launcher)
        self.assertNotIn(".hermes/.env", launcher)

    def test_run_agent_script_is_installed_and_reachable_through_sudo(self) -> None:
        bootstrap = render._render_bootstrap()
        self.assertIn("\n  run-agent-script\n", bootstrap)
        self.assertIn("/usr/local/lib/kern-host/run-agent-script", bootstrap)

    def test_run_agent_script_scope_outlives_the_host_side_turn_timeout(self) -> None:
        # The scope limit is the backstop, so it must sit behind the admin
        # API's own timeout rather than pre-empting its clearer message.
        from host.agent_scripts import (
            SCRIPT_SCOPE_MAX_SECONDS,
            SCRIPT_TIMEOUT_SECONDS,
        )

        launcher = Path("host/bootstrap/helpers/run-agent-script.sh").read_text()
        self.assertIn(f"RuntimeMaxSec={SCRIPT_SCOPE_MAX_SECONDS}", launcher)
        self.assertGreater(SCRIPT_SCOPE_MAX_SECONDS, SCRIPT_TIMEOUT_SECONDS)
        self.assertIn("BindsTo=kern-admin-api.service", launcher)

    def test_run_agent_script_admits_only_a_script_path_in_the_agent_home(self) -> None:
        # Root validates the spelling before it builds anything; the file
        # checks belong to the demoted side and are not exercised here.
        raw = Path("host/bootstrap/helpers/run-agent-script.sh").read_text().replace(
            "@PROXY_PORT@", "7445"
        )
        harness = raw.replace(
            "cd /mnt/kern-agent/agent-home", "cd /"
        ).replace("exec systemd-run", "exec echo systemd-run")
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh") as handle:
            handle.write(harness)
            script_path = handle.name
        self.addCleanup(lambda: Path(script_path).unlink(missing_ok=True))

        def forwarded(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", script_path, *args], capture_output=True, text=True, check=False
            )

        accepted = forwarded(
            "--thread-scope",
            "schedule-3-run-7",
            "/mnt/kern-agent/agent-home/scripts/backup.sh",
        )
        self.assertEqual(accepted.returncode, 0)
        self.assertIn("--unit kern-agent-thread-schedule-3-run-7", accepted.stdout)
        self.assertIn("runuser -u kern-agent", accepted.stdout)
        # The path reaches bash as one positional argument, never as part of a
        # command string, so its spelling cannot become syntax.
        self.assertTrue(
            accepted.stdout.rstrip().endswith(
                "run-agent-script /mnt/kern-agent/agent-home/scripts/backup.sh"
            ),
            accepted.stdout,
        )

        for rejected in (
            (),
            ("/etc/cron.daily/backup.sh",),
            ("/mnt/kern-agent/agent-home/backup",),
            ("/mnt/kern-agent/agent-home/../../etc/backup.sh",),
            ("/mnt/kern-agent/agent-home/scripts/../backup.sh",),
            ("/mnt/kern-agent/agent-home/./backup.sh",),
            ("/mnt/kern-agent/agent-home/backup.sh", "extra"),
            ("--thread-scope", "bad id", "/mnt/kern-agent/agent-home/backup.sh"),
            ("-rf", "/mnt/kern-agent/agent-home/backup.sh"),
        ):
            with self.subTest(args=rejected):
                result = forwarded(*rejected)
                self.assertEqual(result.returncode, 64)
                self.assertEqual(result.stdout, "")

    def test_run_claude_code_launcher_combines_web_search_and_thread_scope(self) -> None:
        # The launcher — not its caller — translates the operator's web-search
        # decision into the WebSearch deny. Neutralize the parts that need root
        # and host paths so we can observe exactly what it forwards to claude.
        raw = Path("host/bootstrap/helpers/run-claude-code.sh").read_text().replace("@PROXY_PORT@", "7445")
        harness = raw.replace(
            "cd /mnt/kern-agent/agent-home", "cd /"
        ).replace("exec systemd-run", "exec echo systemd-run")
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh") as handle:
            handle.write(harness)
            script_path = handle.name
        self.addCleanup(lambda: Path(script_path).unlink(missing_ok=True))

        def forwarded(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", script_path, *args], capture_output=True, text=True, check=False
            )

        off = forwarded(
            "web-search=off",
            "--thread-scope",
            "sample_app__ws-3",
            "-p",
            "hello",
        )
        self.assertEqual(off.returncode, 0)
        self.assertIn('--settings', off.stdout)
        self.assertIn('{"permissions":{"deny":["WebSearch"]}}', off.stdout)
        # The decision arg is consumed, not forwarded; the rest passes through.
        self.assertIn("-p", off.stdout)
        self.assertIn("hello", off.stdout)
        self.assertNotIn("web-search=off", off.stdout)
        self.assertIn("--unit kern-agent-thread-sample_app__ws-3", off.stdout)
        self.assertNotIn("--thread-scope", off.stdout)
        self.assertIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", off.stdout)

        on = forwarded("web-search=on", "-p", "hello")
        self.assertEqual(on.returncode, 0)
        self.assertNotIn("--settings", on.stdout)
        self.assertNotIn("WebSearch", on.stdout)
        self.assertIn("hello", on.stdout)
        self.assertIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", on.stdout)

        # Pinned Claude Code hides every account-limit window when this flag
        # is set. /usage is host-owned maintenance, so it alone runs without
        # the suppression while retaining the WebSearch deny.
        usage = forwarded("web-search=off", "-p", "/usage", "--output-format", "json")
        self.assertEqual(usage.returncode, 0)
        self.assertIn("/usage", usage.stdout)
        self.assertIn("WebSearch", usage.stdout)
        self.assertNotIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", usage.stdout)

        missing = forwarded("auth", "login")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("web-search=on or web-search=off", missing.stderr)

        invalid_scope = forwarded("web-search=off", "--thread-scope", "not/valid", "-p", "hello")
        self.assertNotEqual(invalid_scope.returncode, 0)
        self.assertIn("invalid --thread-scope thread id", invalid_scope.stderr)

    def test_upgrade_helper_has_one_fixed_bounded_source(self) -> None:
        helper = Path("host/bootstrap/helpers/check-for-upgrade.sh").read_text()

        self.assertIn(
            "https://raw.githubusercontent.com/infiloop2/kern/refs/heads/main/VERSION",
            helper,
        )
        self.assertIn("--proto '=https'", helper)
        self.assertIn("--max-time 10", helper)
        self.assertIn("--max-filesize 64", helper)
        self.assertNotIn("$@", helper)

    def test_agent_file_helper_skips_entries_that_disappear_during_listing(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            stable = home_path / "stable.txt"
            stable.write_text("stable")
            namespace = self._agent_file_helper_namespace(home_path)
            test_case = self

            class StableEntry:
                name = "stable.txt"

                def is_symlink(self) -> bool:
                    return False

                def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
                    test_case.assertFalse(follow_symlinks)
                    return stable.stat()

            class VanishedEntry:
                name = "vanished.txt"

                def is_symlink(self) -> bool:
                    return False

                def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
                    test_case.assertFalse(follow_symlinks)
                    raise FileNotFoundError("vanished")

            output = io.StringIO()
            with patch("os.scandir", return_value=FakeScandir([VanishedEntry(), StableEntry()])), patch("sys.stdout", output):
                namespace["list_path"]("/")  # type: ignore[index, operator]
            listed = json.loads(output.getvalue())
            self.assertEqual(listed["path"], "/")
            self.assertEqual([entry["name"] for entry in listed["entries"]], ["stable.txt"])

    def test_agent_file_helper_bounds_directory_scan_work(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            namespace = self._agent_file_helper_namespace(home_path)
            test_case = self

            class StableEntry:
                def __init__(self, name: str) -> None:
                    self.name = name

                def is_symlink(self) -> bool:
                    return False

                def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
                    test_case.assertFalse(follow_symlinks)
                    path = home_path / self.name
                    path.write_text("stable")
                    return path.stat()

            class ExplodingEntry:
                name = "should-not-be-touched.txt"

                def is_symlink(self) -> bool:
                    raise AssertionError("listing inspected past the scan cap")

            entries = [StableEntry(f"file-{index:04d}.txt") for index in range(1000)] + [ExplodingEntry()]
            output = io.StringIO()
            with patch("os.scandir", return_value=FakeScandir(entries)), patch("sys.stdout", output):
                namespace["list_path"]("/")  # type: ignore[index, operator]
            listed = json.loads(output.getvalue())
            self.assertTrue(listed["truncated"])
            self.assertEqual(len(listed["entries"]), 1000)
            self.assertNotIn("should-not-be-touched.txt", {entry["name"] for entry in listed["entries"]})

    def test_agent_file_helper_opens_files_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            namespace = self._agent_file_helper_namespace(home_path)
            calls: list[tuple[object, int, int | None]] = []

            def fake_open(path: object, flags: int, *_, dir_fd: int | None = None) -> int:
                calls.append((path, flags, dir_fd))
                if path == home_path:
                    return 10
                if path == "fifo":
                    raise OSError(errno.ENXIO, "no writer")
                raise AssertionError(f"unexpected open path: {path!r}")

            with patch("os.open", side_effect=fake_open), patch("os.close"):
                with self.assertRaises(OSError):
                    namespace["read_path"]("/fifo")  # type: ignore[index, operator]
            file_open = next(call for call in calls if call[0] == "fifo")
            self.assertNotEqual(file_open[1] & namespace["NONBLOCK"], 0)  # type: ignore[index, operator]

    def test_agent_file_helper_rejects_directory_symlink_as_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
            home_path = Path(home)
            (home_path / "outside-dir-link").symlink_to(outside, target_is_directory=True)
            namespace = self._agent_file_helper_namespace(home_path)

            output = io.StringIO()
            with patch("sys.stdout", output), self.assertRaises(SystemExit) as exc:
                namespace["list_path"]("/outside-dir-link")  # type: ignore[index, operator]

            self.assertEqual(exc.exception.code, 3)
            self.assertIn("symlinks are not supported", output.getvalue())

    def test_agent_file_helper_stream_supports_bounded_images(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            payload = b"mock-png"
            (home_path / "frame.png").write_bytes(payload)
            namespace = self._agent_file_helper_namespace(home_path)

            output = io.BytesIO()
            stdout = io.TextIOWrapper(output, encoding="utf-8")
            with patch("sys.stdout", stdout):
                namespace["stream_path"]("/frame.png")  # type: ignore[index, operator]
                stdout.flush()

            raw_header, streamed = output.getvalue().split(b"\n", 1)
            header = json.loads(raw_header)
            self.assertEqual(header["media_type"], "image/png")
            self.assertEqual(header["size_bytes"], len(payload))
            self.assertEqual(streamed, payload)

    def test_agent_file_helper_stream_rejects_oversized_image(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            oversized = home_path / "huge.webp"
            with oversized.open("wb") as handle:
                handle.truncate(25 * 1024 * 1024 + 1)
            namespace = self._agent_file_helper_namespace(home_path)

            output = io.StringIO()
            with patch("sys.stdout", output), self.assertRaises(SystemExit) as exc:
                namespace["stream_path"]("/huge.webp")  # type: ignore[index, operator]

            self.assertEqual(exc.exception.code, 3)
            self.assertIn("file is larger than 26214400 bytes", output.getvalue())

    def test_agent_file_helper_stream_rejects_unsupported_content(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            (home_path / "payload.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><script>bad()</script></svg>'
            )
            namespace = self._agent_file_helper_namespace(home_path)

            output = io.StringIO()
            with patch("sys.stdout", output), self.assertRaises(SystemExit) as exc:
                namespace["stream_path"]("/payload.svg")  # type: ignore[index, operator]

            self.assertEqual(exc.exception.code, 3)
            self.assertIn("only MP4, MOV, JPEG, PNG, or WebP", output.getvalue())

    def _agent_file_helper_namespace(self, home_path: Path) -> dict[str, object]:
        helper = Path("host/bootstrap/helpers/read-agent-file.sh").read_text()
        body = helper.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        namespace: dict[str, object] = {"__name__": "read_agent_file_test"}
        exec(
            compile(
                body.replace('Path("/mnt/kern-agent/agent-home")', f"Path({str(home_path)!r})"),
                "read-agent-file.py",
                "exec",
            ),
            namespace,
        )
        return namespace

    def test_bootstrap_payload_omits_runtime_network_policy(self) -> None:
        config = sample_input_config()
        payload = deploy._bootstrap_payload(
            config,
            SAMPLE_ADMIN_PASSWORD_SHA256,
            build_operator_connections(SAMPLE_SSH_PUBLIC_KEY, None, None),
            {"admin": "vol-admin", "agent": "vol-agent"},
            mode="deploy",
            target_version="0.1.0",
        )
        self.assertEqual(payload["storage_volumes"], {"admin": "vol-admin", "agent": "vol-agent"})
        self.assertEqual(payload["operation"], {"mode": "deploy", "target_version": "0.1.0", "allow_upgrade": False})
        self.assertEqual(payload["runtime_config"]["agent_name"], "kern-test")
        self.assertEqual(payload["runtime_config"]["admin_password_sha256"], SAMPLE_ADMIN_PASSWORD_SHA256)
        self.assertEqual(
            payload["runtime_config"]["operator_connections"],
            [{"mode": "ssh", "ssh_public_key": "ssh-ed25519 AAAATEST operator@example"}],
        )
        self.assertNotIn("network_controls", payload)

    def test_upgrade_bootstrap_payload_omits_replacement_operator_connections(self) -> None:
        config = sample_input_config()
        payload = deploy._bootstrap_payload(
            config,
            None,
            None,
            {"admin": "vol-admin", "agent": "vol-agent"},
            mode="upgrade",
            target_version="0.1.0",
        )

        self.assertEqual(payload["runtime_config"], {"agent_name": "kern-test"})

    def test_runtime_code_archive_excludes_cli_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "kern-host-code.tar.gz"
            render._write_runtime_code_archive(archive)

            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())

        self.assertIn("host/runtime/admin_api/service.py", names)
        self.assertIn("host/version.py", names)
        # VERSION rides along so self_provision can enforce the version gate
        # on the delivered tree.
        self.assertIn("VERSION", names)
        self.assertIn("host/bootstrap/agent-home/agents_claude.md", names)
        self.assertIn("host/runtime/root_helpers/upload_agent_file.py", names)
        self.assertNotIn("host/bootstrap/agent-home/AGENTS.md", names)
        self.assertNotIn("host/bootstrap/agent-home/CLAUDE.md", names)
        self.assertIn("host/bootstrap/agent-home/.codex/config.toml", names)
        self.assertIn("host/bootstrap/agent-home/.claude/settings.json", names)
        self.assertIn("host/bootstrap/agent-home/.hermes/config.yaml", names)
        # The tools service imports the bundled tool packages at startup; they
        # ship under host/tools inside the host archive.
        self.assertIn("host/tools/host_api.py", names)
        self.assertIn("host/tools/gmail/__init__.py", names)
        self.assertIn("host/tools/shared/google.py", names)
        self.assertNotIn("host/cli", names)
        self.assertFalse(any(name.startswith("host/cli/") for name in names))
        self.assertFalse(any("__pycache__" in name for name in names))


class FakeCliIntegrationTests(unittest.TestCase):
    def test_deploy_provisions_over_ssh_and_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            log_path = tmp_path / "cli_calls.jsonl"
            for name in ("aws", "ssh", "scp", "ssh-keygen"):
                fake = fake_bin / name
                fake.write_text(_fake_cli_script(name, log_path))
                fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "AWS_ACCESS_KEY_ID": "access",
                    "AWS_SECRET_ACCESS_KEY": "secret",
                    "AWS_REGION": "us-east-1",
                }
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "host.cli.deploy",
                    "--agent-name",
                    "kern-test",
                    "--operator-ssh-public-key",
                    SAMPLE_SSH_PUBLIC_KEY,
                    "--admin-password-sha256",
                    SAMPLE_ADMIN_PASSWORD_SHA256,
                ],
                cwd=tmp_path,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            # stdout is exactly the result JSON; progress went to stderr.
            result = json.loads(proc.stdout)
            self.assertIn("[deploy]", proc.stderr)
            self.assertEqual(result["admin_ui_local_url"], "http://127.0.0.1:7443")
            self.assertEqual(result["public_dns"], "kern.example.com")
            self.assertEqual(result["ssh_user"], "kern-operator")
            self.assertEqual(result["admin_volume_id"], "vol-admin")
            self.assertEqual(result["agent_volume_id"], "vol-agent")
            self.assertEqual(result["version"], deploy.repo_version())
            self.assertEqual(result["operator_connections"], [{"mode": "ssh"}])

            calls = [json.loads(line) for line in log_path.read_text().splitlines()]
            run_call = next(call for call in calls if call[1:3] == ["ec2", "run-instances"])
            self.assertIn("--associate-public-ip-address", run_call)
            self.assertIn("subnet-public", run_call)
            self.assertTrue(any(f"Key=kern-host-version,Value={deploy.repo_version()}" in str(item) for item in run_call))
            # User data is passed as fileb:// so the AWS CLI base64-encodes the raw
            # bytes (a raw string would be base64-decoded under cli_binary_format=base64
            # and corrupt the cloud-init script). Content is covered by the render test.
            user_data = run_call[run_call.index("--user-data") + 1]
            self.assertTrue(user_data.startswith("fileb://"))

            volume_creates = [call for call in calls if call[1:3] == ["ec2", "create-volume"]]
            self.assertEqual(len(volume_creates), 2)
            self.assertIn("--volume-type", volume_creates[0])
            self.assertIn("gp3", volume_creates[0])
            self.assertIn("--encrypted", volume_creates[0])
            self.assertEqual(volume_creates[0][volume_creates[0].index("--size") + 1], "16")
            self.assertEqual(volume_creates[1][volume_creates[1].index("--size") + 1], "16")
            volume_attaches = [call for call in calls if call[1:3] == ["ec2", "attach-volume"]]
            self.assertEqual(len(volume_attaches), 2)
            self.assertIn("vol-admin", volume_attaches[0])
            self.assertIn("vol-agent", volume_attaches[1])

            scp_call = next(call for call in calls if call[0] == "scp")
            copied = " ".join(scp_call)
            # The payload rides in user data and bootstrap renders on the
            # host; SSH pushes only the runtime code archive.
            self.assertNotIn("kern_payload.json", copied)
            self.assertNotIn("kern_bootstrap.sh", copied)
            self.assertIn("kern-host-code.tar.gz", copied)

            provision_call = next(
                call for call in calls if call[0] == "ssh" and any("self_provision" in item for item in call)
            )
            remote = next(item for item in provision_call if "self_provision" in item)
            self.assertIn("tar -xzf /tmp/kern-host-code.tar.gz", remote)
            self.assertIn("python3 -m host.bootstrap.self_provision", remote)
            self.assertIn("--payload /tmp/kern_payload.json", remote)


def _fake_cli_script(name: str, log_path: Path) -> str:
    return f"""#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
with open({str(log_path)!r}, "a") as log:
    log.write(json.dumps([{name!r}] + args) + "\\n")

def emit(value):
    print(json.dumps(value))

if {name!r} == "ssh-keygen":
    key = pathlib.Path(args[args.index("-f") + 1])
    key.write_text("fake private key\\n")
    key.with_suffix(".pub").write_text("ssh-ed25519 AAAADEPLOY kern-deploy\\n")
elif {name!r} in ("ssh", "scp"):
    pass
elif args[:2] == ["ec2", "describe-instances"] and "--instance-ids" not in args:
    emit({{"Reservations": []}})
elif args[:2] == ["ec2", "describe-instances"] and "--instance-ids" in args:
    emit({{"Reservations": [{{"Instances": [{{"InstanceId": "i-123", "PublicDnsName": "kern.example.com", "Placement": {{"AvailabilityZone": "us-east-1a"}}}}]}}]}})
elif args[:2] == ["ec2", "describe-volumes"]:
    emit({{"Volumes": []}})
elif args[:2] == ["ec2", "create-volume"]:
    tag_spec = args[args.index("--tag-specifications") + 1]
    if "Value=admin" in tag_spec:
        emit({{"VolumeId": "vol-admin"}})
    elif "Value=agent" in tag_spec:
        emit({{"VolumeId": "vol-agent"}})
    else:
        emit({{"VolumeId": "vol-unknown"}})
elif args[:2] == ["ec2", "attach-volume"]:
    pass
elif args[:2] == ["ec2", "describe-vpcs"]:
    emit({{"Vpcs": [{{"VpcId": "vpc-1"}}]}})
elif args[:2] == ["ec2", "describe-subnets"]:
    emit({{"Subnets": [{{"SubnetId": "subnet-public", "AvailabilityZone": "us-east-1a"}}]}})
elif args[:2] == ["ec2", "describe-route-tables"]:
    emit({{"RouteTables": [{{"Routes": [{{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}}]}}]}})
elif args[:2] == ["ec2", "describe-security-groups"] and "--group-ids" not in args:
    emit({{"SecurityGroups": []}})
elif args[:2] == ["ec2", "create-security-group"]:
    emit({{"GroupId": "sg-1"}})
elif args[:2] == ["ec2", "describe-security-groups"] and "--group-ids" in args:
    emit({{"SecurityGroups": [{{"IpPermissions": [], "IpPermissionsEgress": []}}]}})
elif args[:2] == ["ssm", "get-parameter"]:
    emit({{"Parameter": {{"Value": "ami-123"}}}})
elif args[:2] == ["ec2", "run-instances"]:
    emit({{"Instances": [{{"InstanceId": "i-123"}}]}})
elif args[:2] == ["ec2", "wait"]:
    pass
else:
    emit({{}})
"""

class _StringInput:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self, *args):  # type: ignore[no-untyped-def]
        return self.value


class _StringOutput:
    def __init__(self) -> None:
        self.value = ""

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def write(self, value: str) -> int:
        self.value += value
        return len(value)

    def flush(self) -> None:
        return None


class DeployNetworkTests(unittest.TestCase):
    def test_subnet_requires_active_internet_gateway_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "GatewayId": "igw-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertTrue(lifecycle_aws._subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))

    def test_subnet_rejects_nat_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "NatGatewayId": "nat-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertFalse(lifecycle_aws._subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))


if __name__ == "__main__":
    unittest.main()
