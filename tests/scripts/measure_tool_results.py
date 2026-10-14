"""Offline result-byte examples, not live-provider or model-quality evidence.

Run from the repository root: python3 tests/scripts/measure_tool_results.py BASE_REF
BASE_REF is a trusted local revision (the audit uses 80b1d484).
"""
import ast
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.tools.upwork.responses import structured_result
from host.tools.instagram_ads import _campaign_result


def baseline(path, functions=None):
    source = subprocess.check_output(["git", "show", f"{sys.argv[1]}:{path}"], text=True)
    tree = ast.parse(source)
    if functions:
        tree.body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
                     or isinstance(node, ast.FunctionDef) and node.name in functions]
    namespace = {"__name__": "payload_baseline", "__package__": ".".join(Path(path).parent.parts)}
    exec(compile(tree, path, "exec"), namespace)
    return namespace


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def report(name, before, after):
    old, new = size(before), size(after)
    print(f"| {name} | {old:,} | {new:,} | {(old-new)/old:.1%} |")


old_upwork = baseline("host/tools/upwork/responses.py")["structured_result"]
old_campaign = baseline("host/tools/instagram_ads/__init__.py", {"_json", "_campaign_result"})["_campaign_result"]
credential = {"secret": {"access_token": "PRIVATE_ACCESS_TOKEN", "refresh_token": "PRIVATE_REFRESH_TOKEN"}}
search = {"jobs": [{"id": str(1000 + i), "title": f"Python automation project {i}",
    "description_snippet": "Build and document a reliable data pipeline. " * 12,
    "budget": "$2,500", "job_type": "fixed", "skills": ["Python", "API integration"],
    "client": {"country": "United States", "rating": 4.9, "total_reviews": 12,
               "total_spent": "$25,000", "verification_status": "verified"}}
    for i in range(10)], "hasMore": True, "next_cursor": "page-two"}
with_unknown = json.loads(json.dumps(search))
for row in with_unknown["jobs"]:
    row["client"]["payment_method_status"] = "verified"
job = {"data": {"marketplaceJobPosting": {"id": "1000", "content": {
    "title": "Python automation project", "description": "Build and document a reliable data pipeline. 🙂\n" * 80},
    "screening_questions": ["Describe a similar project."]}}, "can_apply": True,
    "connects_balance": 100, "connects_cost": 21}
dashboard = {"profile": {"name": "Example freelancer", "skills": ["Python"]},
             "messages": [{"room_id": "room-1", "text": "Please review the full brief. " * 40}],
             "connects": {"balance": 100}}
messages = {"hasMore": True, "next_cursor": "older-page", "messages": [
    {"id": f"message-{i}", "text": "Full message body. " * 20} for i in range(10)]}
print("| Synthetic result | Before bytes | After bytes | Reduction |")
print("| --- | ---: | ---: | ---: |")
for label, action, value in [("Upwork search, 10 jobs + new client field", "search_jobs", with_unknown),
                             ("Upwork job + screening questions", "get_job", job),
                             ("Upwork modeled search, 10 jobs", "search_jobs", search),
                             ("Upwork opaque dashboard", "get_dashboard", dashboard),
                             ("Upwork opaque message page", "read_messages", messages)]:
    raw = json.dumps(value)
    report(label, old_upwork(raw, action, credential), structured_result(raw, action, credential))
campaigns = [{"id": str(1000 + i), "account_id": "100", "name": f"Summer campaign {i}",
    "objective": "OUTCOME_ENGAGEMENT", "status": "PAUSED", "effective_status": "PAUSED",
    "lifetime_budget": "2500", "buying_type": "AUCTION", "special_ad_categories": [],
    "issues_info": [{"message": "Awaiting creative review"}]} for i in range(20)]
report("Instagram Ads campaign page, 20 rows",
       {"items": [old_campaign(row) for row in campaigns], "next_cursor": "page-two"},
       {"items": [_campaign_result(row) for row in campaigns], "next_cursor": "page-two"})
