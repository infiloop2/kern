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
HOST = "agent-store.cluster-example.us-east-1.rds.amazonaws.com"


class AuroraSetupTests(unittest.TestCase):
    def test_express_checks_cluster_before_connecting_and_preserves_enabled_data_api(self):
        cluster = {"DBClusterArn": CLUSTER, "Engine": "aurora-postgresql", "InternetAccessGatewayEnabled": True,
                   "IAMDatabaseAuthenticationEnabled": True, "HttpEndpointEnabled": True,
                   "Endpoint": HOST, "MasterUsername": "postgres"}
        for override in ({"DBClusterArn": CLUSTER + "wrong"}, {"Engine": "aurora-mysql"},
                         {"InternetAccessGatewayEnabled": False}, {"IAMDatabaseAuthenticationEnabled": False},
                         {"Endpoint": "host dbname=other"}, {"MasterUsername": "postgres:other"}, {}):
            with self.subTest(override=override), patch.object(setup.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="psql (PostgreSQL) 17.6")), patch.object(setup, "aws", return_value={"DBClusters": [{**cluster, **override}]}) as aws, patch.object(setup, "iam_sql") as sql, patch("builtins.print"):
                if override:
                    with self.assertRaises(ValueError):
                        setup.prepare_express("us-east-1", CLUSTER, "agent_store")
                    sql.assert_not_called()
                else:
                    self.assertEqual(setup.prepare_express("us-east-1", CLUSTER, "agent_store"), (HOST, "postgres"))
                    self.assertEqual(sql.call_count, 2)
                self.assertEqual(aws.call_count, 1)

    def test_express_old_psql_rejected_before_aws_calls(self):
        with patch.object(setup.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="psql (PostgreSQL) 15.14")), patch.object(setup, "aws") as aws:
            with self.assertRaisesRegex(ValueError, "17 or newer"):
                setup.prepare_express("us-east-1", CLUSTER, "agent_store")
            aws.assert_not_called()

    def test_iam_token_failure_never_connects_or_exposes_provider_output(self):
        with patch.object(setup.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="private-token", stderr="private-token")) as run:
            with self.assertRaisesRegex(RuntimeError, "Could not generate") as error:
                setup.iam_sql("us-east-1", HOST, "postgres", "postgres", "SELECT 1;")
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("private-token", str(error.exception))

    def test_iam_timeout_cleans_password_file_and_ignores_inherited_pg_settings(self):
        paths = []
        def run(command, **kwargs):
            if command[0] == "aws":
                return SimpleNamespace(returncode=0, stdout="private:token", stderr="")
            paths.append(Path(kwargs["env"]["PGPASSFILE"]))
            self.assertNotIn("PGPASSWORD", kwargs["env"])
            self.assertNotIn("PGHOSTADDR", kwargs["env"])
            self.assertNotIn("PGSERVICE", kwargs["env"])
            self.assertNotIn("PGOPTIONS", kwargs["env"])
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
            raise subprocess.TimeoutExpired(command, 90, stderr="private:token")
        with patch.dict(os.environ, {"PGPASSWORD": "old", "PGHOSTADDR": "127.0.0.1", "PGSERVICE": "old", "PGOPTIONS": "old", "PGSSLMODE": "disable"}), patch.object(setup.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                setup.iam_sql("us-east-1", HOST, "postgres", "postgres", "SELECT 1;")
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())

    def test_default_databases_rejected_before_aws_calls(self):
        for database in ("postgres", "template0", "template1"):
            with self.subTest(database=database), patch.object(setup.sys, "argv", [str(SCRIPT), "--region", "us-east-1", "--cluster-arn", CLUSTER, "--database", database]), patch.object(setup, "aws") as aws:
                with self.assertRaisesRegex(ValueError, "fresh dedicated database"):
                    setup.main()
                aws.assert_not_called()

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
        self.run_copied_guide(express=False)

    def test_copied_express_guide_uses_admin_session_without_master_secret(self):
        self.run_copied_guide(express=True)

    def test_express_connection_failure_creates_no_database_or_secrets(self):
        self.run_copied_guide(express=True, failure="SELECT 1")

    def test_express_existing_database_failure_creates_no_secrets(self):
        self.run_copied_guide(express=True, failure="CREATE DATABASE")

    def test_express_bootstrap_failure_stops_without_success_or_leaking_passwords(self):
        self.run_copied_guide(express=True, failure="DO $kern_setup$")

    def run_copied_guide(self, *, express, failure=""):
        from host.tools.aurora_postgres.setup import SETUP_COMMAND
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "python3").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
            (root / "python3").chmod(0o700)
            fake_aws = root / "aws"
            fake_aws.write_text("#!" + sys.executable + "\n" + "\n".join([
                "import json, pathlib, sys",
                "if 'generate-db-auth-token' in sys.argv:",
                "    assert sys.argv[sys.argv.index('--hostname') + 1] == " + repr(HOST),
                "    assert sys.argv[sys.argv.index('--username') + 1] == 'postgres'",
                "    print('private-iam-token:with:colons')",
                "    sys.exit(0)",
                "request = pathlib.Path(sys.argv[sys.argv.index('--cli-input-json') + 1][7:])",
                "assert request.stat().st_mode & 0o777 == 0o600",
                "payload = json.loads(request.read_text())",
                "with open('requests.jsonl', 'a') as log: log.write(json.dumps(payload) + '\\n')",
                "if 'describe-db-clusters' in sys.argv:",
                "    print(json.dumps({'DBClusters': [{'DBClusterArn': " + repr(CLUSTER) + ", 'Engine': 'aurora-postgresql', 'InternetAccessGatewayEnabled': True, 'IAMDatabaseAuthenticationEnabled': True, 'HttpEndpointEnabled': False, 'Endpoint': " + repr(HOST) + ", 'MasterUsername': 'postgres'}]}))",
                "else:",
                "    print(json.dumps({'ARN': 'arn:aws:test:secret:' + payload.get('Name', 'bootstrap')}))",
            ]) + "\n")
            fake_aws.chmod(0o700)
            fake_psql = root / "psql"
            fake_psql.write_text("#!" + sys.executable + "\n" + "\n".join([
                "import json, os, pathlib, sys",
                "if '--version' in sys.argv:",
                "    print('psql (PostgreSQL) 17.6')",
                "    sys.exit(0)",
                "assert '-X' in sys.argv and '-w' in sys.argv and '--set=ON_ERROR_STOP=1' in sys.argv and '--file=-' in sys.argv",
                "assert os.environ['PGSSLMODE'] == 'verify-full' and os.environ['PGSSLROOTCERT'] == 'system'",
                "assert not any(key in os.environ for key in ('PGPASSWORD', 'PGHOSTADDR', 'PGSERVICE', 'PGOPTIONS'))",
                "password_file = pathlib.Path(os.environ['PGPASSFILE'])",
                "assert password_file.stat().st_mode & 0o777 == 0o600",
                "database = sys.argv[sys.argv.index('--dbname') + 1]",
                "assert password_file.read_text() == " + repr(HOST + ":5432:") + " + database + ':postgres:private-iam-token\\:with\\:colons\\n'",
                "assert not any('private-iam-token' in value for value in sys.argv + list(os.environ.values()))",
                "sql = sys.stdin.read()",
                "with open('sql.jsonl', 'a') as log: log.write(json.dumps({'sql': sql, 'database': database, 'passfile': str(password_file)}) + '\\n')",
                "if " + repr(failure) + " and " + repr(failure) + " in sql:",
                "    print('private-iam-token ' + sql, file=sys.stderr)",
                "    sys.exit(1)",
                "print('OK')",
            ]) + "\n")
            fake_psql.chmod(0o700)
            result = subprocess.run(["/bin/bash", "-H", "-c", SETUP_COMMAND], input="\n".join(["us-east-1", CLUSTER, "" if express else MASTER, "", ""]) + "\n", cwd=root,
                env={**{key:value for key,value in os.environ.items() if key != "PYTHONPATH"}, "PATH":str(root) + os.pathsep + os.environ.get("PATH", "")},
                capture_output=True, text=True, timeout=15)
            calls = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
            self.assertNotIn("private-iam-token", result.stdout + result.stderr)
            if express:
                sql_calls = [json.loads(line) for line in (root / "sql.jsonl").read_text().splitlines()]
                self.assertTrue(all(not Path(call['passfile']).exists() for call in sql_calls))
            if failure:
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Bootstrap complete", result.stdout)
                self.assertIn("Do not blindly rerun", result.stderr)
                if failure in ("SELECT 1", "CREATE DATABASE"):
                    self.assertFalse(any("SecretString" in call for call in calls))
                    self.assertEqual(len(sql_calls), 1 if failure == "SELECT 1" else 2)
                else:
                    self.assertEqual(len(sql_calls), 3)
                    for call in calls:
                        if "SecretString" in call:
                            self.assertNotIn(json.loads(call["SecretString"])["password"], result.stdout + result.stderr)
                return
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("AWS region: ", result.stdout)
            if express:
                self.assertEqual(calls[0], {"DBClusterIdentifier": CLUSTER})
                self.assertEqual(calls[1], {"ResourceArn": CLUSTER})
                self.assertEqual(sql_calls[0]["sql"], "SELECT 1;")
                self.assertEqual(sql_calls[1]["sql"], 'CREATE DATABASE "agent_store";')
                self.assertEqual(sql_calls[1]["database"], "postgres")
                self.assertEqual(sql_calls[2]["database"], "agent_store")
                bootstrap_sql = sql_calls[2]["sql"]
                secret_calls = calls[2:]
                self.assertEqual(len(calls), 6)
                self.assertFalse(any("secretArn" in call for call in calls))
            else:
                self.assertEqual(len(calls), 5)
                self.assertEqual(calls[-1]["secretArn"], MASTER)
                self.assertEqual(calls[-1]["resourceArn"], CLUSTER)
                self.assertEqual(calls[-1]["database"], "agent_store")
                bootstrap_sql = calls[-1]["sql"]
                secret_calls = calls[:4]
            self.assertIn('CREATE SCHEMA "agent_data"', bootstrap_sql)
            self.assertEqual(len(secret_calls), 4)
            for payload in secret_calls:
                password = json.loads(payload["SecretString"])["password"]
                self.assertIn(password, bootstrap_sql)
                self.assertNotIn(password, result.stdout + result.stderr)
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
