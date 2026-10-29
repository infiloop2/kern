"""Copyable operator setup and IAM policy for the integration guide."""
from pathlib import Path

from .bootstrap import ROLES, bootstrap_statements

# SQL fixture uses concrete names; the guide script generates real passwords.
BOOTSTRAP_SQL = 'DO $kern_setup$\nBEGIN\n' + ';\n'.join(bootstrap_statements('agent_store', 'agent_data', {r: 'REPLACE_WITH_UNIQUE_PASSWORD_' + r for r in ROLES})) + ';\nEND;\n$kern_setup$;'

IAM_POLICY = '''{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect":"Allow", "Action":["rds-data:ExecuteStatement","rds-data:BeginTransaction","rds-data:CommitTransaction","rds-data:RollbackTransaction"],
     "Resource":"arn:aws:rds:us-east-1:123456789012:cluster:agent-store"},
    {"Effect":"Allow", "Action":"secretsmanager:GetSecretValue", "Resource":[
      "REPLACE_WITH_READER_SECRET_ARN", "REPLACE_WITH_WRITER_SECRET_ARN",
      "REPLACE_WITH_DDL_SECRET_ARN", "REPLACE_WITH_DROPPER_SECRET_ARN"]}
  ]
}'''


SETUP_COMMAND = r"""python3 - --region us-east-1 \
  --cluster-arn 'arn:aws:rds:us-east-1:123456789012:cluster:agent-store' \
  --master-secret-arn 'arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!cluster-example-AbCdEf' \
  --database agent_store --schema agent_data <<'KERN_AURORA_SETUP'
""" + Path(__file__).with_name("bootstrap.py").read_text(encoding="utf-8") + "KERN_AURORA_SETUP\n"
