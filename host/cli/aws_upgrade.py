"""Fresh-root upgrades on retained EC2 compute; durable volumes stay attached."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from host.cli import aws_resources as aws
from host.cli.lifecycle_logging import _log
from host.cli.power_aws import _describe_instance, _stop_instance
from host.config import ConfigError, InputConfig


@dataclass(frozen=True)
class RootUpgrade:
    instance: dict[str, Any]
    image: dict[str, Any]
    root: dict[str, Any]
    volumes: dict[str, str]
    network: tuple[str, str, str]


def prepare_upgrade(config: InputConfig, env: dict[str, str], instance_id: str) -> RootUpgrade:
    """Read-only checks before creating a volume or stopping working compute."""
    instance = _describe_instance(env, instance_id)
    if instance["State"]["Name"] not in {"running", "stopped"}:
        raise ConfigError("upgrade requires a running or stopped instance; use recover --allow-upgrade to recreate compute")
    image = aws._aws(env, "ec2", "describe-images", "--image-ids", aws._ubuntu_ami(env))["Images"][0]
    boot = image.get("BootMode", "uefi" if image["Architecture"] == "arm64" else "legacy-bios")
    if (
        image["Architecture"] != instance["Architecture"]
        or image["VirtualizationType"] != instance["VirtualizationType"]
        or (boot != "uefi-preferred" and boot != instance.get("CurrentInstanceBootMode", "legacy-bios"))
        or image.get("TpmSupport") != instance.get("TpmSupport")
    ):
        raise ConfigError("target Ubuntu image is incompatible with retained compute; use recover --allow-upgrade")
    zone = instance["Placement"]["AvailabilityZone"]
    network = (instance["VpcId"], instance["SubnetId"], zone)
    if not aws._subnet_has_public_ipv4_route(env, network[0], network[1]):
        raise ConfigError("existing Kern subnet has no public IPv4 route; restore networking or use recover")
    root_device = instance["RootDeviceName"]
    mappings = {item["DeviceName"]: item["Ebs"]["VolumeId"] for item in instance["BlockDeviceMappings"] if "Ebs" in item}
    if instance.get("RootDeviceType") != "ebs" or root_device not in mappings:
        raise ConfigError("upgrade requires an attached EBS root; use recover --allow-upgrade")
    root = aws._aws(env, "ec2", "describe-volumes", "--volume-ids", mappings[root_device])["Volumes"][0]
    tags = {tag["Key"]: tag["Value"] for tag in root.get("Tags", [])}
    if tags.get(aws.OWNER_TAG_KEY) != "true" or tags.get(aws.INSTANCE_TAG_KEY) != config.agent_name:
        raise ConfigError("existing root volume is not owned by this Kern host; refusing to replace it")
    volumes: dict[str, str] = {}
    for role in ("admin", "agent"):
        volume = aws._find_storage_volume(config, env, role)
        if volume is None or volume["AvailabilityZone"] != zone or not any(
            attachment.get("InstanceId") == instance_id and attachment.get("State") == "attached"
            for attachment in volume.get("Attachments", [])
        ):
            raise ConfigError(f"upgrade requires the preserved {role} volume attached to {instance_id}")
        volumes[role] = volume["VolumeId"]
    if len({root["VolumeId"], *volumes.values()}) != 3:
        raise ConfigError("root, admin and agent volumes must be distinct")
    return RootUpgrade(instance, image, root, volumes, network)


def replace_root(
    config: InputConfig,
    plan: RootUpgrade,
    user_data: str,
    workdir: Path,
    env: dict[str, str],
    target_version: str,
    ssh_ingress: bool,
    cloudflare_egress: bool,
) -> tuple[str, str]:
    instance_id = plan.instance["InstanceId"]
    root_device = plan.instance["RootDeviceName"]
    source = next(item["Ebs"] for item in plan.image["BlockDeviceMappings"]
                  if item["DeviceName"] == plan.image["RootDeviceName"])
    settings = aws._root_volume_settings()
    if settings["VolumeSize"] < source["VolumeSize"]:
        raise ConfigError("configured root volume is smaller than the selected Ubuntu image")
    user_data_path = aws._write_user_data(user_data, workdir)
    encryption = []
    if plan.root.get("Encrypted"):
        encryption = ["--encrypted", "--kms-key-id", plan.root["KmsKeyId"]]
    _log(f"preparing fresh Ubuntu root for {instance_id}")
    new_root = aws._aws(
        env, "ec2", "create-volume", "--snapshot-id", source["SnapshotId"],
        "--availability-zone", plan.network[2], "--size", str(settings["VolumeSize"]),
        "--volume-type", settings["VolumeType"], *encryption,
        "--tag-specifications", aws._tag_spec("volume", config.agent_name),
    )["VolumeId"]
    replacing = False
    old_root = plan.root["VolumeId"]
    try:
        aws._aws(env, "ec2", "wait", "volume-available", "--volume-ids", new_root)
        # Protect data before any failure path is allowed to terminate compute.
        aws._preserve_existing_storage_volumes_on_instance_termination(config, env, [instance_id])
        _log(f"stopping {instance_id}; preserving its CPU credits and data disks")
        _stop_instance(env, instance_id, plan.instance["State"]["Name"])
        group = aws._ensure_security_group(config, env, plan.network[0],
                                          ssh_ingress=ssh_ingress, cloudflare_egress=cloudflare_egress)
        aws._configure_existing_instance(config, env, instance_id, user_data_path, group, target_version)
        replacing = True
        _log(f"replacing root {old_root} with {new_root}")
        aws._aws(env, "ec2", "detach-volume", "--volume-id", old_root, "--instance-id", instance_id)
        aws._aws(env, "ec2", "wait", "volume-available", "--volume-ids", old_root)
        aws._aws(env, "ec2", "attach-volume", "--volume-id", new_root,
                 "--instance-id", instance_id, "--device", root_device)
        aws._aws(env, "ec2", "wait", "volume-in-use", "--volume-ids", new_root)
        aws._aws(env, "ec2", "modify-instance-attribute", "--instance-id", instance_id,
                 "--block-device-mappings", json.dumps([{
                     "DeviceName": root_device, "Ebs": {"DeleteOnTermination": settings["DeleteOnTermination"]},
                 }]))
        aws._aws(env, "ec2", "delete-volume", "--volume-id", old_root)
        old_root = ""
        _log(f"starting {instance_id} on the fresh root")
        aws._aws(env, "ec2", "start-instances", "--instance-ids", instance_id)
        return instance_id, group
    except BaseException:
        if replacing:
            # Match launch/bootstrap failure semantics: no exposed half-install,
            # no rollback of databases already migrated on preserved storage.
            try:
                aws._terminate_instances([instance_id], env)
            except Exception as exc:
                _log(f"warning: failed to terminate incomplete upgrade {instance_id}: {exc}")
        for volume_id in (old_root if replacing else "", new_root):
            if volume_id:
                _delete_unused_root(env, volume_id)
        _log("upgrade failed; use recover --allow-upgrade to rebuild compute from the preserved data disks")
        raise


def _delete_unused_root(env: dict[str, str], volume_id: str) -> None:
    try:
        volumes = aws._aws(env, "ec2", "describe-volumes", "--filters", f"Name=volume-id,Values={volume_id}")["Volumes"]
        if volumes and volumes[0]["State"] == "available":
            aws._aws(env, "ec2", "delete-volume", "--volume-id", volume_id)
    except Exception as exc:
        _log(f"warning: could not clean up disposable root {volume_id}: {exc}")
