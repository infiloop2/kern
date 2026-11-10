"""Resolve Codex reviewed commits and stamp clean verdicts as PR approvals."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

# The action and shared parser are both checked out from trusted main.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ci'))
import check_review

CODEX = 'chatgpt-codex-connector[bot]'
ACTIONS = 'github-actions[bot]'
CLEAN = "Codex Review: Didn't find any major issues."
STAMP = 'Approved automatically: Codex found no major issues in this revision.'


def request(path, body=None):
    command = ['gh', 'api', path]
    if body is not None:
        command += ['--input', '-']
    return json.loads(subprocess.check_output(
        command, input=None if body is None else json.dumps(body).encode(),
    ))


def pages(path):
    result = []
    for page in range(1, 101):
        batch = request(f'{path}?per_page=100&page={page}')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError('Too many review records to validate safely')


def resolve_reviewed_commit(root, ref):
    # The commits API also accepts branch/tag names. Refuse those namespaces
    # so an attacker cannot shadow Codex's hexadecimal abbreviation with a ref.
    for namespace in ('heads', 'tags'):
        if request(f'{root}/git/matching-refs/{namespace}/{ref}'):
            raise RuntimeError('Reviewed SHA conflicts with a branch or tag name')
    return request(f'{root}/commits/{ref}')['sha']


def clean_verdict(head, comments, reviews, resolve_commit):
    """Latest Codex verdict on this SHA wins, including later findings reviews."""
    return check_review.current_verdict(head, comments, reviews, resolve_commit)


def stamp_body(verdict):
    body = f"{STAMP}\n\nCodex verdict: {verdict['html_url']}"
    state = check_review.summary_state(verdict['body'])
    if state is not None:
        # The bot edits one summary comment across commits. Bind its changing
        # contents as well as its stable URL, so later heads can get a stamp
        # without rebinding an earlier approval to a different commit.
        body += f"\nReviewed commit: `{state['code_review']['commit_sha']}`"
    return body


def has_stamp(head, verdict, reviews):
    approvals = [item for item in reviews
                 if item['user']['login'] == ACTIONS and item.get('commit_id') == head
                 and item.get('body', '').startswith(STAMP)]
    if not approvals:
        return False
    latest = max(approvals, key=lambda item: item['id'])
    return latest['state'] == 'APPROVED' and latest['body'] == stamp_body(verdict)


def eligible(pr, repo, head):
    return (pr['state'] == 'open' and not pr['draft']
            and pr['base']['ref'] == 'main'
            and (pr['head'].get('repo') or {}).get('full_name') == repo
            and pr['head']['sha'] == head)


def run():
    repo = os.environ['GITHUB_REPOSITORY']
    root = f'/repos/{repo}'
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    comment = event['comment']
    if (comment['user']['login'] == CODEX
            and comment.get('body', '').startswith(check_review.SUMMARY)
            and not check_review.clean_body(comment['body'])):
        # Summary edits also announce running reviews and findings. They are
        # valid events, but grant no approval and need no failed workflow.
        print('Codex summary is not clean and complete; no approval created.')
        return
    number = event['issue']['number']
    pr_path = f'{root}/pulls/{number}'
    pr = request(pr_path)
    head = pr['head']['sha']
    if not eligible(pr, repo, head):
        raise RuntimeError('Requires an open, ready, same-repository PR to main at the expected SHA')
    comments = pages(f'{root}/issues/{number}/comments')
    reviews = pages(f'{pr_path}/reviews')
    verdict = clean_verdict(
        head, comments, reviews,
        lambda ref: resolve_reviewed_commit(root, ref),
    )
    if not verdict:
        raise RuntimeError('Current PR head does not have a clean Codex verdict')
    if event['comment']['user']['login'] != CODEX or event['comment']['id'] != verdict['id']:
        raise RuntimeError('Event is not the latest clean Codex verdict')
    if any(item['user']['login'] == ACTIONS
           and item.get('body') == stamp_body(verdict)
           and item.get('commit_id') != head for item in reviews):
        raise RuntimeError('This Codex verdict was already bound to another full SHA')
    if not eligible(request(pr_path), repo, head):
        raise RuntimeError('PR changed during review validation')
    if not has_stamp(head, verdict, reviews):
        request(f'{pr_path}/reviews', {
            'commit_id': head, 'event': 'APPROVE', 'body': stamp_body(verdict),
        })
    print(f'Codex approval stamped on {head}.')


if __name__ == '__main__':
    run()
