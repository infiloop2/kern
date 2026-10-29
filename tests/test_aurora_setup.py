"""Operator bootstrap keeps credentials out of argv and stops on AWS failures."""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "host/tools/aurora_postgres/bootstrap.py"
spec = importlib.util.spec_from_file_location("aurora_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


CLUSTER = "arn:aws:rds:us-east-1:123456789012:cluster:agent-store"
MASTER = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!cluster-example-AbCdEf"


class AuroraSetupTests(unittest.TestCase):
    def test_request_file_is_private_removed_and_not_in_argv(self):
        paths = []
        def run(command, **kwargs):
            path = Path(command[command.index("--cli-input-json")+1].removeprefix("file://"))
            paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"SecretString":"private-password"})
            self.assertNotIn("private-password",str(command))
            self.assertTrue(kwargs["capture_output"])
            return SimpleNamespace(returncode=0,stdout='{"ARN":"secret-arn"}',stderr="")
        with patch.object(setup.subprocess,"run",side_effect=run):
            self.assertEqual(setup.aws("us-east-1","secretsmanager","create-secret",{"SecretString":"private-password"}), {"ARN":"secret-arn"})
        self.assertTrue(all(not path.exists() for path in paths))

    def test_failed_secret_creation_stops_before_database_bootstrap(self):
        with patch.object(setup.sys,"argv",[str(SCRIPT),"--region","us-east-1","--cluster-arn",CLUSTER,"--master-secret-arn",MASTER]), patch.object(setup,"aws",side_effect=RuntimeError("AWS request failed")) as aws, patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                setup.main()
        self.assertEqual(aws.call_count,1)
        self.assertEqual(aws.call_args.args[1:3],("secretsmanager","create-secret"))

    def test_generated_passwords_match_secrets_and_sql_but_not_stdout(self):
        calls = []
        def aws(region,service,operation,payload):
            calls.append((service,operation,payload))
            return {"ARN":"arn:"+str(len(calls))}
        with patch.object(setup.sys,"argv",[str(SCRIPT),"--region","us-east-1","--cluster-arn",CLUSTER,"--master-secret-arn",MASTER]), patch.object(setup,"aws",side_effect=aws), patch("builtins.print") as output:
            setup.main()
        self.assertEqual(len(calls),5)
        for _,_,payload in calls[:4]:
            password = json.loads(payload["SecretString"])["password"]
            self.assertIn(password,calls[-1][2]["sql"])
            self.assertNotIn(password,str(output.call_args_list))
        self.assertEqual(calls[-1][2]["secretArn"],MASTER)
        self.assertNotIn(MASTER,str(output.call_args_list))

    def test_reserved_schema_rejected_before_aws_calls(self):
        for schema in ("public", "information_schema", "pg_catalog", "pg_custom"):
            with self.subTest(schema=schema), patch.object(setup.sys, "argv", [str(SCRIPT), "--region", "us-east-1", "--cluster-arn", "cluster", "--master-secret-arn", "master", "--schema", schema]), patch.object(setup, "aws") as aws:
                with self.assertRaisesRegex(ValueError, "dedicated schema"):
                    setup.main()
                aws.assert_not_called()

    def test_copied_guide_command_runs_without_repository(self):
        from host.tools.aurora_postgres.setup import SETUP_COMMAND
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "python3").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
            (root / "python3").chmod(0o700)
            fake_aws = root / "aws"
            fake_aws.write_text("#!" + sys.executable + "\n" + "\n".join([
                "import json, pathlib, sys",
                "request = pathlib.Path(sys.argv[sys.argv.index('--cli-input-json') + 1][7:])",
                "assert request.stat().st_mode & 0o777 == 0o600",
                "payload = json.loads(request.read_text())",
                "with open('requests.jsonl', 'a') as log: log.write(json.dumps(payload) + '\\n')",
                "print(json.dumps({'ARN': 'arn:aws:test:secret:' + payload.get('Name', 'bootstrap')}))",
            ]) + "\n")
            fake_aws.chmod(0o700)
            result = subprocess.run(["/bin/bash", "-H"], input=SETUP_COMMAND, cwd=root,
                env={**{key:value for key,value in os.environ.items() if key != "PYTHONPATH"}, "PATH":str(root) + os.pathsep + os.environ.get("PATH", "")},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
            self.assertEqual(len(calls), 5)
            self.assertEqual(calls[-1]["secretArn"], MASTER)
            self.assertIn("--master-secret-arn '" + MASTER + "'", SETUP_COMMAND)
            for payload in calls[:4]:
                password = json.loads(payload["SecretString"])["password"]
                self.assertIn(password, calls[-1]["sql"])
                self.assertNotIn(password, result.stdout)
            self.assertIn("AURORA_POSTGRES_DROPPER_SECRET_ARN", result.stdout)

    def test_region_and_account_mismatch_rejected_before_aws_calls(self):
        for region, cluster, master in (
            ("eu-west-1", CLUSTER, MASTER),
            ("us-east-1", CLUSTER, MASTER.replace("us-east-1", "eu-west-1")),
            ("us-east-1", CLUSTER, MASTER.replace("123456789012", "999999999999")),
        ):
            with self.subTest(region=region, master=master), patch.object(setup.sys, "argv", [str(SCRIPT), "--region", region, "--cluster-arn", cluster, "--master-secret-arn", master]), patch.object(setup, "aws") as aws:
                with self.assertRaises(ValueError):
                    setup.main()
                aws.assert_not_called()

    def test_secret_names_are_distinct_between_clusters(self):
        names = []
        def aws(region, service, operation, payload):
            if service == "secretsmanager":
                names.append(payload["Name"])
            return {"ARN":"arn:test"}
        for cluster in (CLUSTER, CLUSTER.replace("cluster:agent-store", "cluster:second-store")):
            with patch.object(setup.sys, "argv", [str(SCRIPT), "--region", "us-east-1", "--cluster-arn", cluster, "--master-secret-arn", MASTER]), patch.object(setup, "aws", side_effect=aws), patch("builtins.print"):
                setup.main()
        self.assertEqual(len(set(names)), 8)
        self.assertEqual(names[0], "kern/agent-store/agent_store/kern_reader")
        self.assertEqual(names[4], "kern/second-store/agent_store/kern_reader")
