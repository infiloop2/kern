"""Check an existing PR approval stamp; admin fallback belongs to the action."""
import json
import os
from pathlib import Path
import re
import subprocess

STAMP = 'Approved automatically: Codex found no major issues in this revision.'


def request(path):
    return json.loads(subprocess.check_output(['gh', 'api', path]))


def has_stamp(head, reviews):
    stamps = [review for review in reviews
              if review['user']['login'] == 'github-actions[bot]'
              and review.get('commit_id') == head
              and review.get('body', '').startswith(STAMP + '\n\nCodex verdict: ')]
    return bool(stamps) and max(stamps, key=lambda review: review['id'])['state'] == 'APPROVED'


def eligible(pr, repo, head):
    return (pr['state'] == 'open' and not pr['draft']
            and pr['base']['ref'] == 'main'
            and (pr['head'].get('repo') or {}).get('full_name') == repo
            and pr['head']['sha'] == head)


def authorized():
    if os.environ['GITHUB_EVENT_NAME'] != 'issue_comment':
        return False
    head = os.environ['EXPECTED_SHA']
    if not re.fullmatch('[0-9a-f]{40}', head):
        return False
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    repo = os.environ['GITHUB_REPOSITORY']
    path = f"/repos/{repo}/pulls/{event['issue']['number']}"
    if not eligible(request(path), repo, head):
        return False
    reviews = []
    for page in range(1, 101):
        batch = request(f'{path}/reviews?per_page=100&page={page}')
        reviews.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise RuntimeError('Too many reviews to validate safely')
    return has_stamp(head, reviews) and eligible(request(path), repo, head)


if __name__ == '__main__':
    result = authorized()
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'authorized={str(result).lower()}\n')
