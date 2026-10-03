"""Exercise the trusted policy against GitHub-shaped API responses."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('review_policy', ROOT / '.github/actions/stamp-codex-review/stamp.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
auth_spec = importlib.util.spec_from_file_location('stamp_authorization', ROOT / '.github/actions/authorize-admin-or-codex-stamp/authorize.py')
authorizer = importlib.util.module_from_spec(auth_spec)
auth_spec.loader.exec_module(authorizer)
HEAD = 'a' * 40
REPO = 'infiversehq/kern'
PR = {'state': 'open', 'draft': False, 'base': {'ref': 'main'},
      'head': {'sha': HEAD, 'repo': {'full_name': REPO}}}
VERDICT = {'id': 12, 'user': {'login': policy.CODEX},
           'body': policy.CLEAN + f'\n\n**Reviewed commit:** `{HEAD[:10]}`',
           'updated_at': '2026-10-02T01:00:00Z', 'html_url': 'https://github.com/infiversehq/kern/pull/1#issuecomment-12'}
STAMP = {'id': 20, 'user': {'login': policy.ACTIONS}, 'commit_id': HEAD,
         'state': 'APPROVED', 'body': policy.stamp_body(VERDICT)}


class CodexReviewPolicyTests(unittest.TestCase):
    def invoke(self, *, mode='authorize', comments=None,
               reviews=None, pr=None, final_pr=None, sha=HEAD, event_name='issue_comment',
               event_actor=policy.CODEX, event_id=12, resolved_sha=HEAD, resolution_error=None, shadow_ref=False):
        writes = []
        pr = deepcopy(PR if pr is None else pr)
        reads = 0

        def api(path, body=None):
            nonlocal reads
            if body is not None:
                writes.append((path, body))
                return {}
            if '/git/matching-refs/' in path:
                return [{'ref': 'shadow'}] if shadow_ref else []
            if '/commits/' in path:
                self.assertEqual(path.rsplit('/', 1)[1], HEAD[:10])
                if resolution_error:
                    raise resolution_error
                return {'sha': resolved_sha}
            if path.endswith('/pulls/1'):
                reads += 1
                return pr if reads == 1 or final_pr is None else final_pr
            if '/comments?' in path:
                return [deepcopy(VERDICT)] if comments is None else comments
            if '/reviews?' in path:
                return [deepcopy(STAMP)] if reviews is None else reviews
            self.fail(path)

        with tempfile.TemporaryDirectory() as folder:
            event_path = Path(folder) / 'event.json'
            event_path.write_text(json.dumps({'issue': {'number': 1}, 'comment': {
                'id': event_id, 'user': {'login': event_actor}}}))
            env = {'GITHUB_REPOSITORY': REPO, 'GITHUB_EVENT_PATH': str(event_path),
                   'GITHUB_EVENT_NAME': event_name, 'EXPECTED_SHA': sha,
                   'REQUESTER': 'rerun-requester'}
            module = policy if mode == 'stamp' else authorizer
            with patch.dict(os.environ, env), patch.object(module, 'request', side_effect=api):
                if mode == 'stamp':
                    policy.run()
                else:
                    result = authorizer.authorized()
                    self.assertEqual(writes, [])
                    return result
        return writes

    def test_clean_current_stamp_allows_non_admin_without_writes(self):
        self.assertTrue(self.invoke())

    def test_manual_dispatch_defers_to_admin_action(self):
        self.assertFalse(self.invoke(event_name='workflow_dispatch', comments=[]))

    def test_missing_forged_stale_or_dismissed_stamp_is_denied(self):
        for changes in (None, {'user': {'login': 'someone'}}, {'commit_id': 'b' * 40},
                        {'state': 'DISMISSED'}, {'body': policy.STAMP}):
            with self.subTest(changes=changes):
                self.assertFalse(self.invoke(reviews=[] if changes is None else [{**STAMP, **changes}]))

    def test_missing_forged_stale_quoted_or_negative_verdict_is_denied(self):
        for changes in (None, {'user': {'login': 'someone'}},
                        {'body': policy.CLEAN + '\n**Reviewed commit:** `bbbbbbbbbb`'},
                        {'body': '> ' + VERDICT['body']}, {'body': VERDICT['body'].replace(policy.CLEAN, 'Found issues')}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.invoke(mode='stamp', comments=[] if changes is None else [{**VERDICT, **changes}])

    def test_stamping_checks_verdict_but_authorization_only_checks_stamp(self):
        finding = {'id': 30, 'user': {'login': policy.CODEX}, 'commit_id': HEAD,
                   'state': 'COMMENTED', 'body': 'Found a bug.', 'submitted_at': '2026-10-02T02:00:00Z'}
        with self.assertRaisesRegex(RuntimeError, 'clean Codex verdict'):
            self.invoke(mode='stamp', reviews=[STAMP, finding])
        self.assertTrue(self.invoke(reviews=[STAMP, finding], comments=[]))
        # A fresh clean review supersedes earlier findings.
        finding['submitted_at'] = '2026-10-02T00:00:00Z'
        self.invoke(mode='stamp', reviews=[STAMP, finding])

    def test_fork_closed_draft_wrong_base_or_moved_head_is_denied(self):
        cases = [dict(PR, state='closed'), dict(PR, draft=True), dict(PR, base={'ref': 'dev'}),
                 dict(PR, head={'sha': HEAD, 'repo': {'full_name': 'fork/kern'}}),
                 dict(PR, head={'sha': HEAD, 'repo': None}),
                 dict(PR, head={'sha': 'b' * 40, 'repo': {'full_name': REPO}})]
        for pr in cases:
            with self.subTest(pr=pr):
                self.assertFalse(self.invoke(pr=pr))
        self.assertFalse(self.invoke(final_pr=cases[-1]))

    def test_abbreviated_sha_must_resolve_to_exact_head_before_stamping(self):
        collision = HEAD[:10] + 'b' * 30
        for kwargs in ({'resolved_sha': collision},
                       {'resolution_error': RuntimeError('ambiguous SHA')}, {'shadow_ref': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.invoke(mode='stamp', **kwargs)

    def test_exact_sha_required(self):
        self.assertFalse(self.invoke(sha=''))

    def test_stamp_pins_commit_and_is_idempotent(self):
        writes = self.invoke(mode='stamp', reviews=[])
        self.assertEqual(writes, [(f'/repos/{REPO}/pulls/1/reviews', {
            'commit_id': HEAD, 'event': 'APPROVE', 'body': policy.stamp_body(VERDICT)})])
        self.assertEqual(self.invoke(mode='stamp'), [])

    def test_stamp_never_rebinds_a_verdict_to_a_different_commit(self):
        with self.assertRaisesRegex(RuntimeError, 'already bound'):
            self.invoke(mode='stamp', reviews=[{**STAMP, 'commit_id': HEAD[:10] + 'b' * 30}])

    def test_stamp_rejects_spoofed_or_superseded_event_and_race(self):
        for kwargs in ({'event_actor': 'someone'}, {'event_id': 11},
                       {'final_pr': dict(PR, draft=True)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.invoke(mode='stamp', reviews=[], **kwargs)

    def test_paginated_reviews_and_fail_closed_api_errors(self):
        with patch.object(policy, 'request', side_effect=[[{}] * 100, [STAMP]]) as api:
            self.assertEqual(len(policy.pages('/reviews')), 101)
            self.assertEqual(api.call_args.args[0], '/reviews?per_page=100&page=2')
        with patch.object(policy, 'request', side_effect=RuntimeError('API unavailable')):
            with self.assertRaises(RuntimeError):
                policy.pages('/reviews')

    def test_workflows_pin_smoke_authority_before_checkout_and_use_trusted_stamp_code(self):
        for name in ('kern-smoke', 'test-lima-host'):
            source = (ROOT / f'.github/workflows/{name}.yml').read_text()
            self.assertIn('uses: ./.github/actions/authorize-admin-or-codex-stamp', source)
            self.assertEqual(source.count('uses: ./.github/actions/authorize-admin-or-codex-stamp'), 2)
            self.assertNotIn('path: .workflow-authority', source)
            self.assertIn('expected-sha: ${{ steps.resolve.outputs.ref }}', source)
            self.assertIn('expected-sha: ${{ needs.authorize.outputs.ref }}', source)
            self.assertLess(source.index('Authorize admin or reviewed PR'), source.index('      - name: Mark smoke pending')
                            if name == 'test-lima-host' else source.index('      - name: Reject when smoke is already active'))
        source = (ROOT / '.github/workflows/approve-codex-review.yml').read_text()
        self.assertIn('uses: ./.github/actions/stamp-codex-review', source)
        self.assertIn('ref: main', source)
        self.assertNotIn('pull_request.head', source)
        self.assertNotIn('secrets.', source)
        self.assertIn("github.event.comment.user.login == 'chatgpt-codex-connector[bot]'", source)

    def test_composite_falls_back_to_existing_admin_gate(self):
        source = (ROOT / '.github/actions/authorize-admin-or-codex-stamp/action.yml').read_text()
        self.assertIn("if: ${{ steps.stamp.outputs.authorized != 'true' }}", source)
        self.assertIn('uses: ./.github/actions/authorize-repo-admin', source)
        self.assertNotIn('continue-on-error', source)
        self.assertNotIn('collaborators/', Path(authorizer.__file__).read_text())

    def test_latest_stamp_state_wins_and_review_pages_are_complete(self):
        self.assertFalse(self.invoke(reviews=[STAMP, {**STAMP, 'id': 21, 'state': 'DISMISSED'}]))
        responses = [PR, [{'user': {'login': 'other'}}] * 100, [STAMP], PR]
        with patch.object(authorizer, 'request', side_effect=responses) as api:
            with tempfile.TemporaryDirectory() as folder:
                event = Path(folder) / 'event.json'
                event.write_text(json.dumps({'issue': {'number': 1}}))
                with patch.dict(os.environ, GITHUB_EVENT_NAME='issue_comment', EXPECTED_SHA=HEAD,
                                GITHUB_REPOSITORY=REPO, GITHUB_EVENT_PATH=str(event)):
                    self.assertTrue(authorizer.authorized())
            self.assertIn('page=2', api.call_args_list[2].args[0])
