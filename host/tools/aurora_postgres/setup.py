"""Copyable operator setup and IAM policy for the integration guide."""
from pathlib import Path

from .bootstrap import ROLES, bootstrap_statements

# SQL fixture uses concrete names; the guide script generates real passwords.
BOOTSTRAP_SQL = 'DO $kern_setup$\nBEGIN\n' + ';\n'.join(bootstrap_statements('agent_store', 'agent_data', {r: 'REPLACE_WITH_UNIQUE_PASSWORD_' + r for r in ROLES})) + ';\nEND;\n$kern_setup$;'

IAM_POLICY = '''{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "rds-data:ExecuteStatement",
        "rds-data:BeginTransaction",
        "rds-data:CommitTransaction",
        "rds-data:RollbackTransaction"
      ],
      "Resource": [
        "REPLACE_WITH_CLUSTER_ARN"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": [
        "REPLACE_WITH_READER_SECRET_ARN",
        "REPLACE_WITH_WRITER_SECRET_ARN",
        "REPLACE_WITH_DDL_SECRET_ARN",
        "REPLACE_WITH_DROPPER_SECRET_ARN"
      ]
    }
  ]
}'''


SETUP_COMMAND = r"""(
  printf 'AWS region: '
  IFS= read -r aurora_region || exit 1
  printf 'Cluster ARN: '
  IFS= read -r aurora_cluster_arn || exit 1
  printf 'Master secret ARN [Enter for Express / IAM admin]: '
  IFS= read -r aurora_master_secret_arn || exit 1
  printf 'Database [agent_store]: '
  IFS= read -r aurora_database || exit 1
  printf 'Schema [agent_data]: '
  IFS= read -r aurora_schema || exit 1
  python3 - --region "$aurora_region" \
    --cluster-arn "$aurora_cluster_arn" \
    --master-secret-arn "$aurora_master_secret_arn" \
    --database "${aurora_database:-agent_store}" \
    --schema "${aurora_schema:-agent_data}" <<'KERN_AURORA_SETUP'
""" + Path(__file__).with_name("bootstrap.py").read_text(encoding="utf-8") + "KERN_AURORA_SETUP\n)\n"
