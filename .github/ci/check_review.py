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
    # Current Codex receipts prepend this exact marker; older comments do not.
    body = body.removeprefix("<!-- codex-review-receipt:v1 -->").lstrip()
    return body.startswith(CLEAN)


def clean_review(root, number, head):
    verdicts = []
    for item in pages(f"{root}/issues/{number}/comments"):
        if item["user"]["login"] != CODEX:
            continue
        match = re.search(r"\*\*Reviewed commit:\*\* `([0-9a-f]{10,40})`", item["body"])
        if not match or not head.startswith(match[1]):
            continue
        ref = match[1]
        # The commits endpoint accepts ref names too. Refuse shadowing names,
        # then let GitHub reject ambiguous abbreviated object IDs.
        for namespace in ("heads", "tags"):
            if request(f"{root}/git/matching-refs/{namespace}/{ref}"):
                raise RuntimeError("Reviewed SHA conflicts with a named ref")
        if request(f"{root}/commits/{ref}")["sha"] != head:
            raise RuntimeError("Reviewed abbreviation resolved to another commit")
        verdicts.append((item["updated_at"], item["id"], item))
    for item in pages(f"{root}/pulls/{number}/reviews"):
        if item["user"]["login"] == CODEX and item.get("commit_id") == head:
            verdicts.append((item["submitted_at"], item["id"], item))
    if not verdicts:
        return False
    # Issue-comment IDs and review IDs have no common chronological ordering.
    # GitHub timestamps have second precision: a conflict within the latest
    # second must fail closed until a later unambiguous clean verdict arrives.
    latest_time = max(entry[0] for entry in verdicts)
    return all("updated_at" in item and item.get("state") != "DISMISSED"
               and clean_body(item["body"])
               for timestamp, _, item in verdicts if timestamp == latest_time)


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
