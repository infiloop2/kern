from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from host.runtime.agent_runtime import bedrock_credentials


class BedrockCredentialsTests(unittest.TestCase):
    def test_account_status_reports_a_connected_credential(self) -> None:
        account = {
            "access_key_id": "AKIAEXAMPLEKEY000001",
            "account_id": "123456789012",
        }
        with (
            patch.object(
                bedrock_credentials.state,
                "read_bedrock_access_key_id",
                return_value="AKIAEXAMPLEKEY000001",
            ),
            patch.object(bedrock_credentials.state, "read_bedrock_account", return_value=account),
        ):
            self.assertEqual(
                bedrock_credentials.account_status(),
                ("active", None, account),
            )

    def test_account_status_without_a_credential_awaits_connection(self) -> None:
        with patch.object(
            bedrock_credentials.state, "read_bedrock_access_key_id", return_value=None
        ):
            self.assertEqual(
                bedrock_credentials.account_status(),
                ("awaiting_login", None, None),
            )

    def test_account_status_rejects_inconsistent_metadata(self) -> None:
        with (
            patch.object(
                bedrock_credentials.state,
                "read_bedrock_access_key_id",
                return_value="AKIAEXAMPLEKEY000001",
            ),
            patch.object(
                bedrock_credentials.state,
                "read_bedrock_account",
                return_value={"access_key_id": "AKIADIFFERENTKEY00001"},
            ),
        ):
            status, error, account = bedrock_credentials.account_status()
        self.assertEqual(status, "error")
        self.assertIn("submit the credentials again", error or "")
        self.assertIsNone(account)

    def test_read_attested_identity_passes_creds_via_env_and_parses_json(self) -> None:
        # The helper receives the key pair in its environment; the fake echoes
        # the id it was given, proving the env reached the subprocess.
        command = [
            sys.executable,
            "-c",
            (
                "import json, os; print(json.dumps({"
                "'access_key_id': os.environ['KERN_BEDROCK_AWS_ACCESS_KEY_ID'],"
                " 'account_id': '123456789012', 'arn': 'arn:aws:iam::123456789012:user/hermes'}))"
            ),
        ]
        self.assertEqual(
            bedrock_credentials.read_attested_identity(
                command, credential=("AKIAEXAMPLEKEY000001", "S" * 40)
            ),
            {
                "access_key_id": "AKIAEXAMPLEKEY000001",
                "account_id": "123456789012",
                "arn": "arn:aws:iam::123456789012:user/hermes",
            },
        )

    def test_read_attested_identity_maps_exit_3_to_authentication_error(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys; print('AWS rejected the credential: HTTP 403', file=sys.stderr); sys.exit(3)",
        ]
        with self.assertRaises(bedrock_credentials.BedrockAuthenticationError):
            bedrock_credentials.read_attested_identity(
                command, credential=("AKIAEXAMPLEKEY000001", "S" * 40)
            )


if __name__ == "__main__":
    unittest.main()
