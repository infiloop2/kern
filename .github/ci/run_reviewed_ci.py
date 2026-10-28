"""Start deferred PR tests once after an exact-head clean Codex review.
Use original PR runs so GitHub retains their merge refs and required checks.
Both clean comments and first-attempt completion can reconsider a busy run.
Never automatically retry a second attempt or a normal test failure.
"""
import json
import os
from pathlib import Path
import subprocess

import check_review as policy

WORKFLOWS = {"test-all-host.yml"}


def start(pr, repo):
    root = f"/repos/{repo}"
    number, head, base = pr["number"], pr["head"]["sha"], pr["base"]["sha"]
    if not policy.eligible(pr, repo, head, base) or not policy.clean_review(root, number, head):
        return
    for workflow in sorted(WORKFLOWS):
        runs = policy.pages(f"{root}/actions/workflows/{workflow}/runs?event=pull_request&head_sha={head}", "workflow_runs")
        matches = [run for run in runs if run["head_sha"] == head
                   and any(p["number"] == number and p["base"]["sha"] == base
                           for p in run.get("pull_requests", []))]
        if not matches:
            continue
        run = max(matches, key=lambda item: item["id"])
        # This hard bound is independent of PR-controlled job/step names. Even
        # a modified workflow cannot turn this helper into an automatic loop.
        if run["status"] != "completed" or run["run_attempt"] != 1:
            continue
        jobs = policy.pages(f"{root}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
        gates = [job for job in jobs if job["name"] == "Cheap checks and review gate"]
        if len(gates) != 1 or not any(
            step["name"] == "Wait for clean Codex review" and step["conclusion"] == "failure"
            for step in gates[0].get("steps", [])
        ):
            continue
        if not policy.eligible(policy.request(f"{root}/pulls/{number}"), repo, head, base):
            return
        current = policy.request(f"{root}/actions/runs/{run['id']}")
        if current["status"] != "completed" or current["run_attempt"] != 1:
            continue
        try:
            subprocess.run(["gh", "api", "--method", "POST",
                            f"{root}/actions/runs/{run['id']}/rerun"], check=True)
        except subprocess.CalledProcessError:
            # Concurrent clean/completion events may have already started it.
            # Do not retry a rejected API request or conceal a permission error.
            current = policy.request(f"{root}/actions/runs/{run['id']}")
            if current["status"] == "completed" and current["run_attempt"] == 1:
                raise


def handle(event, event_name, repo):
    root = f"/repos/{repo}"
    if event_name == "issue_comment":
        if (not event["issue"].get("pull_request")
                or event["comment"]["user"]["login"] != policy.CODEX
                or not policy.clean_body(event["comment"]["body"])):
            return
        start(policy.request(f"{root}/pulls/{event['issue']['number']}"), repo)
    elif event_name == "workflow_run":
        run = policy.request(f"{root}/actions/runs/{event['workflow_run']['id']}")
        if (run["event"] != "pull_request" or run["run_attempt"] != 1
                or run["path"].split("@", 1)[0] not in {f".github/workflows/{w}" for w in WORKFLOWS}):
            return
        for pr in run.get("pull_requests", []):
            start(policy.request(f"{root}/pulls/{pr['number']}"), repo)


if __name__ == "__main__":
    handle(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
           os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REPOSITORY"])
