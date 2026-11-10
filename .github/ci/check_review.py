"""Read the exact-head Codex verdict before expensive PR tests and at completion.
Run only as a read-only Actions check, loading this policy from trusted main.
Clean review starts deferred CI once; later retries use GitHub Re-run jobs.
CI verifies a snapshot; the merging agent must recheck review before merging.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

CODEX = "chatgpt-codex-connector[bot]"
CLEAN = "Codex Review: Didn't find any major issues."
SUMMARY = "<!-- codex-pull-request-review-summary -->"


def summary_state(body):
    """Read only the anchored bot metadata, never the rendered summary text."""
    match = re.match(re.escape(SUMMARY) + r"\n<!-- codex-review-state:v1 (\{[^\r\n]*\}) -->", body)
    if not match:
        return None
    try:
        state = json.loads(match[1])
    except ValueError:
        return None
    code = state.get("code_review") if isinstance(state, dict) else None
    if not isinstance(code, dict) or not isinstance(code.get("commit_sha"), str):
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", code["commit_sha"]):
        return None
    return state


def clean_summary(state):
    head = state["code_review"]["commit_sha"]
    for name in ("code_review", "security_review"):
        review = state.get(name)
        # Security review is optional until requested. An active/pending one
        # cannot grant a clean verdict from an earlier completed code review.
        if name == "security_review" and (review is None or isinstance(review, dict)
                and all(review.get(key) is None for key in
                        ("status", "commit_sha", "has_any_findings", "has_unresolved_findings"))):
            continue
        if (not isinstance(review, dict) or review.get("commit_sha") != head
                or review.get("status") != "completed"
                or review.get("has_any_findings") is not False
                or review.get("has_unresolved_findings") is not False):
            return False
    return True


def request(path):
    result = subprocess.run(
        ["gh", "api", path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def pages(path, key=None):
    rows = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        response = request(f"{path}{separator}per_page=100&page={page}")
        batch = response[key] if key else response
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise RuntimeError("Too many GitHub records to validate safely")


def eligible(pr, repo, head, base):
    return (pr["state"] == "open" and not pr["draft"]
            and pr["base"]["ref"] == "main"
            and (pr["head"].get("repo") or {}).get("full_name") == repo
            and pr["head"]["sha"] == head and pr["base"]["sha"] == base)


def clean_body(body):
    if body.startswith(SUMMARY):
        state = summary_state(body)
        return state is not None and clean_summary(state)
    # Current Codex receipts prepend this exact marker; older comments do not.
    body = body.removeprefix("<!-- codex-review-receipt:v1 -->").lstrip()
    return body.startswith(CLEAN)


def current_verdict(head, comments, reviews, resolve_commit):
    """Return the latest unambiguous clean bot comment for this full SHA."""
    verdicts = []
    for item in comments:
        if item["user"]["login"] != CODEX:
            continue
        if item["body"].startswith(SUMMARY):
            state = summary_state(item["body"])
            if state is not None and state["code_review"]["commit_sha"] != head:
                continue
            verdicts.append((item["updated_at"], item["id"], item,
                             state is not None and clean_summary(state)))
            continue
        match = re.search(r"\*\*Reviewed commit:\*\* `([0-9a-f]{10,40})`", item["body"])
        if not match or not head.startswith(match[1]):
            continue
        ref = match[1]
        if resolve_commit(ref) != head:
            raise RuntimeError("Reviewed abbreviation resolved to another commit")
        verdicts.append((item["updated_at"], item["id"], item, clean_body(item["body"])))
    for item in reviews:
        if item["user"]["login"] == CODEX and item.get("commit_id") == head:
            verdicts.append((item["submitted_at"], item["id"], item, False))
    if not verdicts:
        return None
    # Issue-comment IDs and review IDs have no common chronological ordering.
    # GitHub timestamps have second precision: a conflict within the latest
    # second must fail closed until a later unambiguous clean verdict arrives.
    latest_time = max(entry[0] for entry in verdicts)
    latest = [entry for entry in verdicts if entry[0] == latest_time]
    if not all(clean and item.get("state") != "DISMISSED" for _, _, item, clean in latest):
        return None
    return max(latest, key=lambda entry: entry[1])[2]


def clean_review(root, number, head):
    def resolve(ref):
        # Refuse named refs shadowing hexadecimal abbreviations. Full SHAs in
        # structured summaries are compared directly, without ref resolution.
        for namespace in ("heads", "tags"):
            if request(f"{root}/git/matching-refs/{namespace}/{ref}"):
                raise RuntimeError("Reviewed SHA conflicts with a named ref")
        return request(f"{root}/commits/{ref}")["sha"]
    return current_verdict(head, pages(f"{root}/issues/{number}/comments"),
                           pages(f"{root}/pulls/{number}/reviews"), resolve) is not None


def gate(event, repo):
    pr = event["pull_request"]
    number, head, base = event["number"], pr["head"]["sha"], pr["base"]["sha"]
    root = f"/repos/{repo}"
    if not eligible(request(f"{root}/pulls/{number}"), repo, head, base):
        raise RuntimeError("PR closed, became a draft, or changed head/base; use its latest PR run")
    ready = clean_review(root, number, head)
    if not eligible(request(f"{root}/pulls/{number}"), repo, head, base):
        raise RuntimeError("PR changed during review verification")
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["gate", "verify"])
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repo = os.environ["GITHUB_REPOSITORY"]
    ready = gate(event, repo)
    if args.mode == "verify" and not ready:
        raise RuntimeError("Current revision no longer has a clean Codex review")
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(f"ready={str(ready).lower()}\n")


if __name__ == "__main__":
    main()
