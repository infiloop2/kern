"""Guest storage-adapter contract tests.

These run without AWS, Lima, or real block devices.  Provider-specific input
parsing and discovery stay in their adapters; the dispatcher owns only the
fixed registry and the two-device role contract consumed by bootstrap.sh.
"""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from host.bootstrap import storage_aws, storage_lima, storage_resolver


def _block_stat() -> Mock:
    return Mock(st_mode=stat.S_IFBLK | 0o600)


class StorageResolverContractTests(unittest.TestCase):
    def test_dispatches_only_the_selected_fixed_adapter(self) -> None:
        aws = Mock(return_value={"admin": "/dev/nvme1n1", "agent": "/dev/nvme2n1"})
        lima = Mock(side_effect=AssertionError("unselected adapter must not run"))
        payload = {
            "storage": {
                "resolver": "aws",
                "resolver_input": {
                    "admin": {"volume_id": "vol-admin"},
                    "agent": {"volume_id": "vol-agent"},
                },
            }
        }
        with patch.dict(storage_resolver._RESOLVERS, {"aws": aws, "lima": lima}, clear=True):
            self.assertEqual(
                storage_resolver.resolve_payload(payload),
                {"admin": "/dev/nvme1n1", "agent": "/dev/nvme2n1"},
            )
        aws.assert_called_once_with(payload["storage"]["resolver_input"])
        lima.assert_not_called()

    def test_rejects_missing_unknown_and_malformed_specs(self) -> None:
        invalid = (
            None,
            {},
            {"storage": None},
            {"storage": {}},
            {"storage": {"resolver": "third-party", "resolver_input": {}}},
            {"storage": {"resolver": "aws", "resolver_input": None}},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                storage_resolver.resolve_payload(payload)

    def test_rejects_wrong_roles_duplicate_devices_and_non_device_paths(self) -> None:
        payload = {"storage": {"resolver": "aws", "resolver_input": {}}}
        invalid_results = (
            {"admin": "/dev/a"},
            {"admin": "/dev/a", "agent": "/dev/a"},
            {"admin": "relative", "agent": "/dev/b"},
            {"admin": "/tmp/a", "agent": "/dev/b"},
            {"admin": "/dev/a", "agent": "/dev/b", "extra": "/dev/c"},
        )
        for result in invalid_results:
            with self.subTest(result=result), patch.dict(
                storage_resolver._RESOLVERS,
                {"aws": Mock(return_value=result)},
                clear=True,
            ), self.assertRaises(ValueError):
                storage_resolver.resolve_payload(payload)


class AwsStorageAdapterTests(unittest.TestCase):
    INPUTS = {
        "admin": {"volume_id": "vol-0123"},
        "agent": {"volume_id": "vol-4567"},
    }

    def test_resolves_each_volume_by_stable_nvme_id_and_verifies_block_device(self) -> None:
        def exists(path: Path) -> bool:
            return str(path).endswith(("vol0123", "vol4567"))

        def realpath(path: object) -> str:
            return "/dev/nvme1n1" if str(path).endswith("vol0123") else "/dev/nvme2n1"

        with patch.object(Path, "exists", autospec=True, side_effect=exists), patch(
            "host.bootstrap.storage_aws.os.path.realpath", side_effect=realpath
        ), patch("host.bootstrap.storage_aws.os.stat", return_value=_block_stat()), patch(
            "host.bootstrap.storage_aws.time.sleep"
        ) as sleep:
            self.assertEqual(
                storage_aws.resolve(self.INPUTS),
                {"admin": "/dev/nvme1n1", "agent": "/dev/nvme2n1"},
            )
        sleep.assert_not_called()

    def test_accepts_aws_by_id_variant_with_hyphens(self) -> None:
        def exists(path: Path) -> bool:
            return str(path).endswith(("vol-0123", "vol-4567"))

        with patch.object(Path, "exists", autospec=True, side_effect=exists), patch(
            "host.bootstrap.storage_aws.os.path.realpath",
            side_effect=["/dev/nvme1n1", "/dev/nvme2n1"],
        ), patch("host.bootstrap.storage_aws.os.stat", return_value=_block_stat()), patch(
            "host.bootstrap.storage_aws.time.sleep"
        ):
            self.assertEqual(set(storage_aws.resolve(self.INPUTS)), {"admin", "agent"})

    def test_waits_boundedly_then_fails_when_volume_is_absent(self) -> None:
        with patch.object(Path, "exists", return_value=False), patch(
            "host.bootstrap.storage_aws.time.sleep"
        ) as sleep:
            with self.assertRaisesRegex(ValueError, "could not find attached EBS"):
                storage_aws.resolve(self.INPUTS)
        self.assertEqual(sleep.call_count, 30)

    def test_rejects_missing_role_input_and_non_block_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent has no volume_id"):
            storage_aws.resolve({"admin": {"volume_id": "vol-a"}})
        with patch.object(Path, "exists", return_value=True), patch(
            "host.bootstrap.storage_aws.os.path.realpath", return_value="/dev/not-block"
        ), patch(
            "host.bootstrap.storage_aws.os.stat", return_value=Mock(st_mode=stat.S_IFREG)
        ):
            with self.assertRaisesRegex(ValueError, "not a block device"):
                storage_aws.resolve(self.INPUTS)


class LimaStorageAdapterTests(unittest.TestCase):
    INPUTS = {
        "admin": {"disk_name": "kern-test-admin"},
        "agent": {"disk_name": "kern-test-agent"},
    }

    def _metadata(self, value: object) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "lima-disks.json"
        path.write_text(json.dumps(value))
        self.addCleanup(directory.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(storage_lima, "_METADATA_PATH", path).start()
        return directory

    def test_resolves_exact_names_independent_of_attachment_order(self) -> None:
        self._metadata(
            {
                "disks": [
                    {"name": "kern-test-agent", "device": "/dev/vdc"},
                    {"name": "unrelated", "device": "/dev/vdd"},
                    {"name": "kern-test-admin", "device": "/dev/vdb"},
                ]
            }
        )
        with patch("host.bootstrap.storage_lima.os.stat", return_value=_block_stat()):
            self.assertEqual(
                storage_lima.resolve(self.INPUTS),
                {"admin": "/dev/vdb", "agent": "/dev/vdc"},
            )

    def test_rejects_missing_duplicate_and_non_block_devices(self) -> None:
        cases = (
            ({}, "does not contain a disk list"),
            ({"disks": []}, "exactly one Lima disk"),
            (
                {
                    "disks": [
                        {"name": "kern-test-admin", "device": "/dev/vdb"},
                        {"name": "kern-test-admin", "device": "/dev/vdc"},
                    ]
                },
                "found 2",
            ),
        )
        for metadata, message in cases:
            with self.subTest(metadata=metadata):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "lima-disks.json"
                    path.write_text(json.dumps(metadata))
                    with patch.object(storage_lima, "_METADATA_PATH", path):
                        with self.assertRaisesRegex(ValueError, message):
                            storage_lima.resolve(self.INPUTS)

        self._metadata(
            {
                "disks": [
                    {"name": "kern-test-admin", "device": "/dev/vdb"},
                    {"name": "kern-test-agent", "device": "/dev/vdc"},
                ]
            }
        )
        with patch(
            "host.bootstrap.storage_lima.os.stat", return_value=Mock(st_mode=stat.S_IFREG)
        ):
            with self.assertRaisesRegex(ValueError, "not a block device"):
                storage_lima.resolve(self.INPUTS)


if __name__ == "__main__":
    unittest.main()
