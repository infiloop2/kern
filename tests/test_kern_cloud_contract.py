"""Hardcoded requirements of Kern Cloud in Infiverse; static checks only.

Reviewed against Infiverse dbfd3ea7c566f87e4a1a74224c036ceafbfe5696.
See docs/development/kern-cloud-contract.md for the dependency analysis.
"""
# IMPORTANT FOR FUTURE AGENTS: Do not update this test under normal circumstances
# or weaken it merely to make a Kern PR pass. Any intentional contract change
# MUST include the corresponding Infiverse support update before release.

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def source(path: str, function: str | None = None) -> str:
    """Ignore comments/formatting without importing or executing Kern."""
    tree = ast.parse((ROOT / path).read_text(), feature_version=(3, 11))
    if function:
        tree, = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function]
    return ast.unparse(tree)


class KernCloudContractTests(unittest.TestCase):
    def test_cli_entrypoints_and_flags(self) -> None:
        lifecycle = source('host/cli/lifecycle.py', '_parse_args')
        power = source('host/cli/power.py', 'main_for_power_mode')
        for verb in ('deploy', 'upgrade', 'reconfigure', 'start', 'stop'):
            with self.subTest(verb=verb):
                dispatcher = 'main_for_power_mode' if verb in {'start', 'stop'} else 'main_for_mode'
                self.assertIn(f"return {dispatcher}('{verb}', argv)", source(f'host/cli/{verb}.py', 'main'))
        self.assertIn("if command.provider == 'aws':\n        from host.cli import lifecycle_aws\n        provider = lifecycle_aws", source('host/cli/lifecycle.py', 'main_for_mode'))
        self.assertIn("if args.provider == 'aws':\n        from host.cli import power_aws\n        provider = power_aws", power)
        self.assertIn('return provider.main_for_power(mode, args.agent_name)', power)
        pin_assignments = [line.strip() for line in lifecycle.splitlines() if line.strip().startswith('github_commit_sha = ')]
        self.assertEqual(pin_assignments, ['github_commit_sha = args.bootstrap_from_github', 'github_commit_sha = github_commit_sha.strip()'])
        self.assertIn('    github_commit_sha = args.bootstrap_from_github\n    if github_commit_sha is not None:\n        github_commit_sha = github_commit_sha.strip()', lifecycle)
        for text in (lifecycle, power):
            self.assertIn("parser.add_argument('--agent-name', required=True,", text)
            self.assertIn("parser.add_argument('--provider', choices=('aws', 'lima'), default='aws',", text)
            for line in text.splitlines():
                if 'parser.add_argument(' in line and 'required=True' in line:
                    self.assertTrue(line.strip().startswith(("parser.add_argument('--agent-name',", "parser.add_argument('--admin-password-sha256',")), line)
        # Unconditional declaration: deploy, upgrade and reconfigure all accept the pin.
        self.assertTrue(any(line.startswith("    parser.add_argument('--bootstrap-from-github',") for line in lifecycle.splitlines()))
        for signature in (
            "'--bootstrap-from-github', nargs='?', const='', metavar='COMMIT_SHA'",
            "'--operator-ssh-public-key', metavar='OPENSSH_PUBLIC_KEY'",
            "'--operator-cloudflare-hostname', metavar='HOSTNAME'",
            "'--admin-password-sha256', required=True, metavar='HEX_DIGEST'",
            "'--reset-admin-passkeys', action='store_true'",
        ):
            self.assertIn(f'parser.add_argument({signature},', lifecycle)
            self.assertLess(lifecycle.index(f'parser.add_argument({signature},'), lifecycle.index('args = parser.parse_args(argv)'))
        endpoint_branches = [node for node in ast.walk(ast.parse(lifecycle)) if isinstance(node, ast.If) and ast.unparse(node.test) == "mode in {'deploy', 'reconfigure'}"]
        endpoint_options = {statement.value.args[0].value for branch in endpoint_branches for statement in branch.body if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call) and ast.unparse(statement.value.func) == 'parser.add_argument'}
        self.assertEqual(endpoint_options, {'--operator-ssh-public-key', '--operator-cloudflare-hostname', '--admin-password-sha256'})
        self.assertIn("if mode == 'reconfigure':\n        parser.add_argument('--reset-admin-passkeys'", lifecycle)
        for forwarded in (
            'mode=mode', 'agent_name=args.agent_name', 'provider=args.provider',
            'github_commit_sha=github_commit_sha', 'admin_password_sha256=admin_password_sha256',
            "operator_ssh_public_key=getattr(args, 'operator_ssh_public_key', None)",
            "operator_cloudflare_hostname=getattr(args, 'operator_cloudflare_hostname', None)",
            "reset_admin_passkeys=bool(getattr(args, 'reset_admin_passkeys', False))",
        ):
            self.assertIn(forwarded, lifecycle)

    def test_environment_and_github_delivery(self) -> None:
        env = source('host/cli/aws_resources.py', '_aws_env')
        for fragment in ("os.environ.get('AWS_ACCESS_KEY_ID')", "os.environ.get('AWS_SECRET_ACCESS_KEY')", 'os.environ.copy()'):
            self.assertIn(fragment, env)
        # Nothing may drop temporary credentials between copying and returning.
        self.assertTrue(env.endswith("    env = os.environ.copy()\n    env['AWS_REGION'] = config.aws_region\n    env['AWS_DEFAULT_REGION'] = config.aws_region\n    return env"))
        self.assertIn("os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')", source('host/cli/lifecycle_aws.py', '_aws_region_from_env'))
        lifecycle = source('host/cli/lifecycle_aws.py', '_main_for_lifecycle_locked')
        for fragment in (
            'if github_commit_sha is not None:\n            github_commit_sha, target_version = _resolve_github_pin(github_commit_sha)',
            'os.environ.get(OPERATOR_TUNNEL_TOKEN_ENV_NAME)',
            'if github_commit_sha is not None:\n                    user_data = _render_github_user_data(payload, github_commit_sha)',
            'replacement_operator_connections = build_operator_connections(command.operator_ssh_public_key, command.operator_cloudflare_hostname, os.environ.get(OPERATOR_TUNNEL_TOKEN_ENV_NAME) if command.operator_cloudflare_hostname is not None else None)',
            '_bootstrap_payload(config.agent_name, admin_password_sha256, replacement_operator_connections,',
            'reset_admin_passkeys=command.reset_admin_passkeys',
            'admin_password_sha256 = command.admin_password_sha256',
            'if deploy_key is not None:\n                    _provision_over_ssh(public_dns, deploy_key, workdir)',
        ):
            self.assertIn(fragment, lifecycle)
        self.assertIn("runtime_config['operator_connections'] = [connection.to_json() for connection in replacement_operator_connections]", source('host/bootstrap/render.py', '_bootstrap_payload'))
        self.assertEqual([line.strip() for line in lifecycle.splitlines() if line.strip().startswith('admin_password_sha256 = ')], ['admin_password_sha256 = command.admin_password_sha256'])
        self.assertIn("if admin_password_sha256 is not None:\n        runtime_config['admin_password_sha256'] = admin_password_sha256", source('host/bootstrap/render.py', '_bootstrap_payload'))
        pin = source('host/cli/lifecycle_aws.py', '_resolve_github_pin')
        self.assertEqual(pin.count('input('), 1)  # Cloud supplies exactly one y\n.
        for fragment in ('pinned_version != cli_version', 'answer = input()', "answer.strip().lower() not in {'y', 'yes'}", 'return (commit_sha, pinned_version)'):
            self.assertIn(fragment, pin)

    def test_result_fields(self) -> None:
        lifecycle = source('host/cli/lifecycle_aws.py', '_main_for_lifecycle_locked')
        power = source('host/cli/power_aws.py', '_result')
        power_main = source('host/cli/power_aws.py', '_main_for_power_locked')
        self.assertIn("    if isinstance(public_dns, str) and public_dns:\n        result['public_dns'] = public_dns", power)
        self.assertIn("if not instance.get('PublicDnsName'):\n            raise ConfigError", source('host/cli/power_aws.py', '_start_instance'))
        for role in ('admin', 'agent'):
            self.assertIn(f"    if {role}_volume is not None:\n        result['{role}_volume_id'] = {role}_volume['VolumeId']", power)
        for text, expected in ((lifecycle, 'print(json.dumps(result, indent=2, sort_keys=True))'), (power_main, 'print(json.dumps(_result(config, env, final, mode=mode, initial_state=initial_state), indent=2, sort_keys=True))')):
            stdout = [line.strip() for line in text.splitlines() if line.strip().startswith('print(') and 'file=sys.stderr' not in line]
            self.assertEqual(stdout, [expected])
            self.assertNotIn('sys.stdout', text)
            self.assertNotIn('os.write(', text)
        for text in (lifecycle, power):
            for field in ('agent_name', 'region', 'instance_id', 'public_dns', 'admin_volume_id', 'agent_volume_id'):
                self.assertIn(repr(field), text)
            self.assertNotIn("'admin_password'", text)
            for fragment in ("'agent_name': config.agent_name", "'region': config.aws_region"):
                self.assertIn(fragment, text)
        for fragment in (
            "'instance_id': instance_id", "'public_dns': public_dns", "'version': target_version",
            "'admin_volume_id': storage_volumes['admin']", "'agent_volume_id': storage_volumes['agent']",
            "if github_commit_sha is not None:\n            result['github_source'] = f'{PUBLIC_GITHUB_REPOSITORY}@{github_commit_sha}'",
            'print(json.dumps(result, indent=2, sort_keys=True))',
        ):
            self.assertIn(fragment, lifecycle)
        for fragment in ("'instance_id': instance['InstanceId']", "result['public_dns'] = public_dns", "result['admin_volume_id'] = admin_volume['VolumeId']", "result['agent_volume_id'] = agent_volume['VolumeId']"):
            self.assertIn(fragment, power)

    def test_release_and_reviewed_iam_policy(self) -> None:
        version = (ROOT / 'VERSION').read_text().strip()
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        self.assertGreaterEqual(tuple(map(int, version.split('.'))), (1, 0, 0))
        policy = json.loads((ROOT / 'iam_policy.json').read_text())
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        # Digest of Cloud's separately reviewed approved_iam_policy.json.
        self.assertEqual(digest, '9a6b6705b955d7eb767001e46b2a1dec13894b1dc802c70d18feb931acf95857')
        for declaration in ("PUBLIC_GITHUB_REPOSITORY = 'infiloop2/kern'", "OPERATOR_TUNNEL_TOKEN_ENV_NAME = 'KERN_CLOUDFLARE_TUNNEL_TOKEN'", 'ADMIN_API_PORT = 7443'):
            self.assertIn(declaration, source('host/constants.py').splitlines())

    def test_cloud_resource_ownership_tags(self) -> None:
        for declaration in ("OWNER_TAG_KEY = 'kern-host'", "INSTANCE_TAG_KEY = 'kern-host-agent-name'"):
            self.assertIn(declaration, source('host/cli/aws_constants.py').splitlines())
        tags = source('host/cli/aws_resources.py', '_resource_tags')
        for fragment in ("f'Key={INSTANCE_TAG_KEY},Value={agent_name}'", "f'Key={OWNER_TAG_KEY},Value=true'"):
            self.assertIn(fragment, tags)
        self.assertIn('_resource_tags(agent_name,', source('host/cli/aws_resources.py', '_tag_spec'))
        self.assertIn('_resource_tags(config.agent_name, target_version)', source('host/cli/aws_resources.py', '_configure_existing_instance'))
        self.assertIn("return f'ResourceType=volume,Tags=[{{Key={INSTANCE_TAG_KEY},Value={agent_name}}},{{Key={OWNER_TAG_KEY},Value=true}},{{Key={VOLUME_ROLE_TAG_KEY},Value={role}}},{{Key=Name,Value=kern-host-{agent_name}-{role}}}]'", source('host/cli/aws_resources.py', '_volume_tag_spec'))
        for function, call in (
            ('_launch_instance', "_tag_spec('instance', config.agent_name, target_version=target_version)"),
            ('_launch_instance', "_tag_spec('volume', config.agent_name)"),
            ('_create_storage_volume', '_volume_tag_spec(config.agent_name, role)'),
            ('_ensure_security_group', "_tag_spec('security-group', config.agent_name)"),
        ):
            self.assertIn(call, source('host/cli/aws_resources.py', function))

    def test_public_login_status(self) -> None:
        service = source('host/runtime/admin_api/service.py')
        dispatch = source('host/runtime/admin_api/service.py', '_handle')
        self.assertLess(dispatch.index("if method == 'GET' and path.path == '/v1/login/status':"), dispatch.index('principal = self._authenticate()'))
        self.assertIn('PORT = ADMIN_API_PORT', service.splitlines())
        self.assertIn('httpd = BoundedThreadingHTTPServer((HOST, PORT), Handler)', service)
        self.assertIn("self._send_json(HTTPStatus.OK, {'passkey_configured': admin_auth.passkey_login_configured()})", source('host/runtime/admin_api/service.py', '_handle_login_status'))
        self.assertIn("if method == 'GET' and path.path == '/v1/login/status':\n                self._handle_login_status()\n                return", source('host/runtime/admin_api/service.py'))
        self.assertIn("('GET', '/v1/login/status')", source('host/runtime/admin_api/admin_auth.py'))


if __name__ == '__main__':
    unittest.main()
