"""Retained-compute lifecycle: real orchestration against a stateful EC2 boundary."""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host.cli import aws_resources as aws, aws_upgrade as upgrade
from host.config import ConfigError, build_input_config


class Ec2:
    def __init__(self, state="running"):
        self.calls = []
        self.fail = None
        self.user_data = None
        self.instance = {
            "InstanceId": "i-old", "State": {"Name": state}, "Architecture": "x86_64",
            "VirtualizationType": "hvm", "CurrentInstanceBootMode": "legacy-bios",
            "VpcId": "vpc-1", "SubnetId": "subnet-1", "Placement": {"AvailabilityZone": "us-east-1a"},
            "RootDeviceType": "ebs", "RootDeviceName": "/dev/sda1",
            "BlockDeviceMappings": [
                {"DeviceName": device, "Ebs": {"VolumeId": volume, "DeleteOnTermination": True}}
                for device, volume in [("/dev/sda1", "vol-old"), ("/dev/sdf", "vol-admin"), ("/dev/sdg", "vol-agent")]
            ],
        }
        self.image = {
            "Architecture": "x86_64", "VirtualizationType": "hvm", "RootDeviceName": "/dev/sda1",
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": "snap-ubuntu", "VolumeSize": 8}}],
        }
        self.volumes = {
            vid: {"VolumeId": vid, "State": "in-use", "AvailabilityZone": "us-east-1a",
                  "Attachments": [{"InstanceId": "i-old", "State": "attached"}],
                  "Tags": [{"Key": "kern-host", "Value": "true"}, {"Key": "kern-host-agent-name", "Value": "kern-test"}]}
            for vid in ("vol-old", "vol-admin", "vol-agent")
        }

    def __call__(self, env, service, action, *args):
        self.calls.append((action, *args))
        if action == self.fail:
            self.fail = None
            raise RuntimeError("injected " + action)
        value = lambda flag: args[args.index(flag) + 1]
        if action == "describe-instances":
            return {"Reservations": [{"Instances": [copy.deepcopy(self.instance)]}]}
        if action == "get-parameter":
            return {"Parameter": {"Value": "ami-ubuntu"}}
        if action == "describe-images":
            return {"Images": [self.image]}
        if action == "describe-volumes":
            if "--volume-ids" in args:
                ids = args[args.index("--volume-ids") + 1:]
            else:
                selector = value("--filters")
                if "volume-id" in selector:
                    ids = [selector.split("Values=")[1]]
                else:
                    role = next(arg.split("Values=")[1] for arg in args if "kern-host-volume-role" in arg)
                    ids = ["vol-" + role]
            return {"Volumes": [self.volumes[vid] for vid in ids if vid in self.volumes]}
        if action == "create-volume":
            self.volumes["vol-new"] = {"VolumeId": "vol-new", "State": "available", "Attachments": []}
            return {"VolumeId": "vol-new"}
        if action == "stop-instances":
            self.instance["State"]["Name"] = "stopped"
        if action == "start-instances":
            self.instance["State"]["Name"] = "running"
        if action == "detach-volume":
            assert self.instance["State"]["Name"] == "stopped"
            vid = value("--volume-id")
            assert vid == "vol-old", "never detach preserved data"
            self.volumes[vid].update(State="available", Attachments=[])
            self.instance["BlockDeviceMappings"] = [m for m in self.instance["BlockDeviceMappings"] if m["Ebs"]["VolumeId"] != vid]
        if action == "attach-volume":
            assert value("--device") == "/dev/sda1"
            vid = value("--volume-id")
            self.volumes[vid].update(State="in-use", Attachments=[{"InstanceId": "i-old", "State": "attached"}])
            self.instance["BlockDeviceMappings"].append({"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": vid, "DeleteOnTermination": False}})
        if action == "modify-instance-attribute":
            if "--attribute" in args:
                assert self.instance["State"]["Name"] == "stopped"
                self.user_data = base64.b64decode(Path(value("--value").removeprefix("file://")).read_bytes())
            if "--block-device-mappings" in args:
                for update in json.loads(value("--block-device-mappings")):
                    mapping = next(m for m in self.instance["BlockDeviceMappings"] if m["DeviceName"] == update["DeviceName"])
                    mapping["Ebs"].update(update["Ebs"])
        if action == "terminate-instances":
            self.instance["State"]["Name"] = "terminated"
            for mapping in self.instance["BlockDeviceMappings"]:
                vid = mapping["Ebs"]["VolumeId"]
                if mapping["Ebs"]["DeleteOnTermination"]:
                    del self.volumes[vid]
                else:
                    self.volumes[vid].update(State="available", Attachments=[])
        if action == "delete-volume":
            vid = value("--volume-id")
            assert vid not in {"vol-admin", "vol-agent"}
            assert self.volumes[vid]["State"] == "available"
            del self.volumes[vid]
        return {}


class RootUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.config = build_input_config("kern-test", "us-east-1")
        self.ec2 = Ec2()
        self.enterContext(patch.object(aws, "_aws", side_effect=self.ec2))
        self.enterContext(patch("host.cli.power_aws._aws", side_effect=self.ec2))
        self.enterContext(patch.object(aws, "_subnet_has_public_ipv4_route", return_value=True))
        self.group = self.enterContext(patch.object(aws, "_ensure_security_group", return_value="sg-current"))
        self.workdir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def run_upgrade(self, *, temporary_ssh_ingress=False):
        plan = upgrade.prepare_upgrade(self.config, {}, "i-old")
        return upgrade.replace_root(self.config, plan, "#!/bin/bash\necho fresh\n", self.workdir, {}, "2.0.0", True, False,
                                    temporary_ssh_ingress=temporary_ssh_ingress)

    def test_failed_swap_closes_only_temporary_ssh(self):
        for temporary in (False, True):
            with self.subTest(temporary=temporary):
                self.ec2.__init__()
                self.ec2.fail = "attach-volume"
                with patch.object(aws, "_close_security_group_ssh_ingress") as close:
                    with self.assertRaisesRegex(RuntimeError, "injected attach-volume"):
                        self.run_upgrade(temporary_ssh_ingress=temporary)
                self.assertEqual(close.call_count, int(temporary))
                if temporary:
                    close.assert_called_once_with({}, "sg-current")
                self.assertEqual(self.ec2.instance["State"]["Name"], "stopped")

    def test_running_and_stopped_retain_compute_and_data_apply_settings_then_boot(self):
        for state in ("running", "stopped"):
            with self.subTest(state=state):
                self.ec2.__init__(state)
                self.assertEqual(self.run_upgrade(), ("i-old", "sg-current"))
                self.assertEqual(self.ec2.instance["State"]["Name"], "running")
                self.assertEqual(set(self.ec2.volumes), {"vol-new", "vol-admin", "vol-agent"})
                self.assertEqual(self.ec2.user_data, b"#!/bin/bash\necho fresh\n")
                calls = self.ec2.calls
                actions = [c[0] for c in calls]
                self.assertNotIn("terminate-instances", actions)
                self.assertNotIn("run-instances", actions)
                self.assertEqual(actions.count("stop-instances"), int(state == "running"))
                self.assertLess(actions.index("create-volume"), actions.index("detach-volume"))
                self.assertLess(actions.index("modify-instance-credit-specification"), actions.index("detach-volume"))
                self.assertLess(actions.index("delete-volume"), actions.index("start-instances"))
                mappings = {m["Ebs"]["VolumeId"]: m["Ebs"]["DeleteOnTermination"] for m in self.ec2.instance["BlockDeviceMappings"]}
                self.assertEqual(mappings, {"vol-new": True, "vol-admin": False, "vol-agent": False})
                settings = aws._instance_settings()
                type_call = next(c for c in calls if "--instance-type" in c)
                self.assertEqual(json.loads(type_call[-1]), {"Value": settings["InstanceType"]})
                credits = next(c for c in calls if c[0] == "modify-instance-credit-specification")
                self.assertEqual(json.loads(credits[-1]), [{"InstanceId": "i-old", **settings["CreditSpecification"]}])
                self.assertEqual((self.workdir / "user_data.base64").stat().st_mode & 0o777, 0o600)

    def test_unsupported_states_require_explicit_recovery_without_writes(self):
        for state in ("pending", "stopping", "shutting-down", "terminated"):
            with self.subTest(state=state):
                self.ec2.__init__(state)
                with self.assertRaisesRegex(ConfigError, "use recover"):
                    self.run_upgrade()
                self.assertEqual([c[0] for c in self.ec2.calls], ["describe-instances"])

    def test_incompatible_image_and_missing_data_abort_before_any_writes(self):
        for change in ("architecture", "boot", "tpm", "volume", "ownership"):
            with self.subTest(change=change):
                self.ec2.__init__()
                if change == "architecture":
                    self.ec2.image["Architecture"] = "arm64"
                elif change == "boot":
                    self.ec2.image["BootMode"] = "uefi"
                elif change == "tpm":
                    self.ec2.image["TpmSupport"] = "v2.0"
                elif change == "volume":
                    self.ec2.volumes["vol-agent"]["Attachments"] = []
                else:
                    self.ec2.volumes["vol-old"]["Tags"] = []
                with self.assertRaises(ConfigError):
                    self.run_upgrade()
                self.assertTrue(all(c[0].startswith("describe-") or c[0] == "get-parameter" for c in self.ec2.calls))

    def test_failures_before_swap_keep_original_instance_and_disks(self):
        self.ec2.fail = "modify-instance-credit-specification"
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.run_upgrade()
        self.assertEqual(self.ec2.instance["State"]["Name"], "stopped")
        self.assertEqual(set(self.ec2.volumes), {"vol-old", "vol-admin", "vol-agent"})
        self.assertNotIn("detach-volume", [c[0] for c in self.ec2.calls])

    def test_swap_failures_stop_incomplete_compute_and_keep_data(self):
        for action in ("detach-volume", "attach-volume", "delete-volume", "start-instances"):
            with self.subTest(action=action):
                self.ec2.__init__()
                self.ec2.fail = action
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    self.run_upgrade()
                self.assertEqual(self.ec2.instance["State"]["Name"], "stopped")
                self.assertTrue({"vol-admin", "vol-agent"}.issubset(self.ec2.volumes))
                self.assertNotIn("terminate-instances", [call[0] for call in self.ec2.calls])
                if action != "start-instances":
                    self.assertIn("vol-old", self.ec2.volumes)
                if action == "attach-volume":
                    self.assertEqual(self.ec2.volumes["vol-old"]["State"], "available")
                    self.assertNotIn("vol-new", self.ec2.volumes)

    def test_credit_api_partial_failure_is_not_treated_as_success(self):
        original = self.ec2.__call__
        def respond(env, service, action, *args):
            if action == "modify-instance-credit-specification":
                return {"UnsuccessfulInstanceCreditSpecifications": [{"InstanceId": "i-old", "Error": {"Code": "InvalidInstanceID"}}]}
            return original(env, service, action, *args)
        with patch.object(aws, "_aws", side_effect=respond):
            with self.assertRaisesRegex(ConfigError, "failed to apply CPU credit settings"):
                self.run_upgrade()
        self.assertEqual(self.ec2.instance["State"]["Name"], "stopped")
        self.assertEqual(set(self.ec2.volumes), {"vol-old", "vol-admin", "vol-agent"})

    def test_replacement_root_preserves_encryption_key(self):
        self.ec2.volumes["vol-old"].update(Encrypted=True, KmsKeyId="arn:aws:kms:us-east-1:123:key/root")
        self.run_upgrade()
        create = next(c for c in self.ec2.calls if c[0] == "create-volume")
        self.assertIn("--encrypted", create)
        self.assertEqual(create[create.index("--kms-key-id") + 1], "arn:aws:kms:us-east-1:123:key/root")
        self.assertEqual(create[create.index("--snapshot-id") + 1], "snap-ubuntu")

    def test_snapshot_wait_failure_cleans_new_root_without_stopping_instance(self):
        self.ec2.fail = "wait"
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.run_upgrade()
        self.assertEqual(self.ec2.instance["State"]["Name"], "running")
        self.assertEqual(set(self.ec2.volumes), {"vol-old", "vol-admin", "vol-agent"})
