"""Typed Upwork results from captured reads and documented result fields.

Unknown fields or changed types preserve the complete redacted provider JSON in
provider_details_json. They are never silently discarded or coerced. That fallback
can be narrowed after observing more response shapes in staging.
"""

import json
import re
from typing import cast

from host.tools.json_types import JSONObject
from host.tools.shared import outputs
from host.tools.host_api import StoredCredential
from host.tools.upwork.mcp_http import MAX_BYTES

ACCOUNTS = outputs.obj({
    'accounts': outputs.array_of(outputs.obj({
        'name': outputs.text("Name."),
        'org_uid': outputs.text("Org uid."),
        'role': outputs.text("Role."),
        'role_label': outputs.text("Role label.")
    }, ["org_uid"]), "Provider records.")
})

JOBS = outputs.obj({
    'client_rating_basis': outputs.text("Client rating basis."),
    'hasMore': outputs.boolean("Has more."),
    'jobs': outputs.array_of(outputs.obj({
        'budget': outputs.text("Budget."),
        'client': outputs.obj({
            'country': outputs.text("Country."),
            'rating': outputs.number("Rating."),
            'total_reviews': outputs.integer("Total reviews."),
            'total_spent': outputs.text("Total spent."),
            'verification_status': outputs.text("Verification status.")
        }),
        'created_date': outputs.text("Created date."),
        'description_snippet': outputs.text("Description snippet."),
        'duration': outputs.text("Duration."),
        'experience_level': outputs.text("Experience level."),
        'freelancers_to_hire': outputs.integer("Freelancers to hire."),
        'id': outputs.text("Id."),
        'job_type': outputs.text("Job type."),
        'proposal_count': outputs.integer("Proposal count."),
        'published_date': outputs.text("Published date."),
        'skills': outputs.array_of(outputs.text("Provider value."), "Provider records."),
        'title': outputs.text("Title."),
        'url': outputs.text("Url.")
    }), "Provider records."),
    'next_cursor': outputs.text("Next cursor."),
    'pageInfo': outputs.obj({
        'endCursor': outputs.text("End cursor."),
        'hasNextPage': outputs.boolean("Has next page.")
    }),
    'status': outputs.text("Status.")
})

JOB = outputs.obj({
    'bid_stats_basis': outputs.obj({
        'contract_type': outputs.text("Contract type."),
        'note': outputs.text("Note.")
    }),
    'can_apply': outputs.boolean("Can apply."),
    'client_record': outputs.obj({
        'contracts_active': outputs.integer("Contracts active."),
        'contracts_total': outputs.integer("Contracts total."),
        'feedback_count': outputs.integer("Feedback count."),
        'feedback_score': outputs.number("Feedback score."),
        'hours_total': outputs.number("Hours total."),
        'jobs_with_hires': outputs.integer("Jobs with hires."),
        'spend_total': outputs.text("Spend total.")
    }),
    'client_work_history': outputs.obj({
        'closed': outputs.array_of(outputs.obj({
            'status': outputs.text("Contract status."),
            'ended': outputs.text("Ended."),
            'feedback_score': outputs.number("Feedback score."),
            'feedback_to_client_score': outputs.number("Feedback to client score."),
            'started': outputs.text("Started."),
            'title': outputs.text("Title."),
            'type': outputs.text("Type.")
        }), "Provider records."),
        'more_available': outputs.boolean("More available."),
        'note': outputs.text("Note."),
        'open': outputs.array_of(outputs.obj({
            'started': outputs.text("Started."),
            'status': outputs.text("Status."),
            'title': outputs.text("Title."),
            'type': outputs.text("Type.")
        }), "Provider records."),
        'selection': outputs.text("Selection."),
        'shown': outputs.integer("Shown.")
    }),
    'connects_balance': outputs.integer("Connects balance."),
    'connects_cost': outputs.integer("Connects cost."),
    'data': outputs.obj({
        'marketplaceJobPosting': outputs.obj({
            'activityStat': outputs.obj({
                'applicationsBidStats': outputs.obj({
                    'avgRateBid': outputs.obj({
                        'currency': outputs.text("Currency."),
                        'displayValue': outputs.text("Display value."),
                        'rawValue': outputs.text("Raw value.")
                    }),
                    'maxRateBid': outputs.obj({
                        'currency': outputs.text("Currency."),
                        'displayValue': outputs.text("Display value."),
                        'rawValue': outputs.text("Raw value.")
                    }),
                    'minRateBid': outputs.obj({
                        'currency': outputs.text("Currency."),
                        'displayValue': outputs.text("Display value."),
                        'rawValue': outputs.text("Raw value.")
                    })
                }),
                'jobActivity': outputs.obj({
                    'invitesSent': outputs.integer("Invites sent."),
                    'totalHired': outputs.integer("Total hired."),
                    'totalInvitedToInterview': outputs.integer("Total invited to interview."),
                    'totalOffered': outputs.integer("Total offered."),
                    'totalUnansweredInvites': outputs.integer("Total unanswered invites.")
                })
            }),
            'canClientReceiveContractProposal': outputs.boolean("Can client receive contract proposal."),
            'classification': outputs.obj({
                'category': outputs.obj({
                    'entityStatus': outputs.text("Entity status."),
                    'id': outputs.text("Id."),
                    'ontologyId': outputs.text("Ontology id."),
                    'preferredLabel': outputs.text("Preferred label."),
                    'type': {"type": "null"}
                }),
                'subCategory': outputs.obj({
                    'entityStatus': outputs.text("Entity status."),
                    'id': outputs.text("Id."),
                    'ontologyId': outputs.text("Ontology id."),
                    'preferredLabel': outputs.text("Preferred label."),
                    'type': {"type": "null"}
                })
            }),
            'clientCompanyPublic': outputs.obj({
                'country': outputs.obj({
                    'IDVerificationDocuments': {"type": "null"},
                    'MivipIDVerificationDocuments': {"type": "null"},
                    'name': outputs.text("Name."),
                    'region': outputs.text("Region.")
                }),
                'id': outputs.text("Id."),
                'state': outputs.text("State."),
                'timezone': outputs.text("Timezone.")
            }),
            'clientProposals': outputs.obj({}),
            'content': outputs.obj({
                'description': outputs.text("Description."),
                'title': outputs.text("Title.")
            }),
            'contractTerms': outputs.obj({
                'contractType': outputs.text("Contract type."),
                'experienceLevel': outputs.text("Experience level."),
                'fixedPriceContractTerms': outputs.obj({
                    'amount': outputs.obj({
                        'currency': outputs.text("Currency."),
                        'displayValue': outputs.text("Display value."),
                        'rawValue': outputs.text("Raw value.")
                    })
                }),
                'personsToHire': outputs.integer("Persons to hire.")
            }),
            'id': outputs.text("Id."),
            'url': outputs.text("Url."),
            'workFlowState': outputs.obj({
                'status': outputs.text("Status.")
            })
        })
    }),
    'plan': outputs.text("Plan."),
    'preferred_locations': outputs.obj({
        'countries': outputs.array_of(outputs.text("Provider value."), "Provider records."),
        'location_required': outputs.boolean("Location required.")
    }),
    'preferred_qualifications': outputs.obj({
        'contractor_type': outputs.text("Contractor type."),
        'english_proficiency': outputs.text("English proficiency."),
        'has_portfolio': outputs.boolean("Has portfolio."),
        'min_earnings': outputs.text("Min earnings."),
        'min_hours_worked': outputs.integer("Min hours worked."),
        'min_job_success_score': outputs.integer("Min job success score."),
        'rising_talent': outputs.boolean("Rising talent.")
    }),
    'status': outputs.text("Status.")
})

PROPOSALS = outputs.obj({
    'data': outputs.obj({
        'vendorProposals': outputs.obj({
            'edges': outputs.array_of(outputs.obj({
                'node': outputs.obj({
                    'auditDetails': outputs.obj({
                        'createdDateTime': outputs.obj({
                            'displayValue': outputs.text("Display value."),
                            'rawValue': outputs.text("Raw value.")
                        }),
                        'modifiedDateTime': outputs.obj({
                            'displayValue': outputs.text("Display value."),
                            'rawValue': outputs.text("Raw value.")
                        })
                    }),
                    'boosted': outputs.boolean("Boosted."),
                    'id': outputs.text("Id."),
                    'marketplaceJobPosting': outputs.obj({
                        'content': outputs.obj({
                            'title': outputs.text("Title.")
                        }),
                        'id': outputs.text("Id."),
                        'url': outputs.text("Url.")
                    }),
                    'status': outputs.obj({
                        'status': outputs.text("Status."),
                        'status_label': outputs.text("Status label.")
                    }),
                    'terms': outputs.obj({
                        'chargeRate': outputs.obj({
                            'currency': outputs.text("Currency."),
                            'displayValue': outputs.text("Display value."),
                            'rawValue': outputs.text("Raw value.")
                        }),
                        'estimatedDuration': outputs.obj({
                            'id': outputs.text("Id."),
                            'label': outputs.text("Label.")
                        })
                    })
                })
            }), "Provider records."),
            'pageInfo': outputs.obj({
                'endCursor': outputs.text("End cursor."),
                'hasNextPage': outputs.boolean("Has next page.")
            }),
            'totalCount': outputs.integer("Total count.")
        })
    }),
    'hasMore': outputs.boolean("Has more."),
    'status': outputs.text("Status."),
    'totalCount': outputs.integer("Total count.")
})

INVITATIONS = outputs.obj({
    'data': outputs.obj({
        'invitations': outputs.array_of(outputs.obj({
            'id': outputs.text('Invitation ID.'),
            'jobPosting': outputs.obj({'id': outputs.text('Job ID for get_job.'), 'title': outputs.text('Job title.')})
        }), "Provider records."),
        'pageInfo': outputs.obj({
            'endCursor': outputs.text("End cursor.")
        }),
        'totalCount': outputs.integer("Total count.")
    }),
    'hasMore': outputs.boolean("Has more."),
    'status': outputs.text("Status."),
    'totalCount': outputs.integer("Total count.")
})

# These fields are named by the authenticated reference; their surrounding
# envelopes have not all been observed. Preserve unmodeled data via the fallback.
HIGHLIGHTS = outputs.obj({
    "certificates": outputs.array_of(outputs.obj({"id": outputs.text("Certificate ID."), "name": outputs.text("Certificate name.")}), "Available certificates."),
    "portfolio_projects": outputs.array_of(outputs.obj({"id": outputs.text("Project ID."), "title": outputs.text("Project title.")}), "Available portfolio projects."),
})
PAGING: JSONObject = {
    "hasMore": outputs.boolean("More results are available; follow the returned cursor."),
    "next_cursor": outputs.text("Cursor for the next page."),
}
BASE_SCHEMAS = {
    "list_accounts": ACCOUNTS,
    "search_jobs": JOBS,
    "get_recommended_jobs": JOBS,
    "get_job": JOB,
    "list_proposals": PROPOSALS,
    "list_invitations": INVITATIONS,
    "get_profile": outputs.obj({}),
    "get_dashboard": outputs.obj({}),
    "get_profile_highlights": HIGHLIGHTS,
    "get_connects_balance": outputs.obj(PAGING),
    "get_proposal": outputs.obj({}),
    "list_conversations": outputs.obj(PAGING),
    "read_messages": outputs.obj({**PAGING, "message_count": outputs.integer("Total messages, only reported after the whole thread has been read.")}),
}

# Require the identifying result path, including every containing object.
# Shapes still awaiting live observation use the nonempty full-JSON fallback.
_CORE_PATHS = {
    "list_accounts": (("accounts",),),
    "search_jobs": (("jobs",),),
    "get_recommended_jobs": (("jobs",),),
    "get_job": (("data", "marketplaceJobPosting", "id"), ("data", "marketplaceJobPosting", "content", "title"), ("data", "marketplaceJobPosting", "content", "description")),
    "list_proposals": (("data", "vendorProposals", "edges"),),
    "list_invitations": (("data", "invitations"),),
    "get_profile_highlights": (("certificates",), ("portfolio_projects",)),
}
for _action, _paths in _CORE_PATHS.items():
    for _path in _paths:
        _schema = BASE_SCHEMAS[_action]
        for _field in _path:
            _required = cast(list[str], _schema.setdefault("required", []))
            if _field not in _required:
                _required.append(_field)
            _schema = cast(JSONObject, cast(JSONObject, _schema["properties"])[_field])


def output_schema(action: str) -> JSONObject:
    return outputs.obj({
        **cast(JSONObject, BASE_SCHEMAS[action]["properties"]),
        "provider_details_json": outputs.text("Full Upwork response when some fields are not typed yet."),
    }, cast(list[str], BASE_SCHEMAS[action].get("required", ["provider_details_json"])))


def redact_text(text: str, credential: StoredCredential) -> str:
    """Check nested JSON escapes as well as literal credential values."""
    values = sorted({v for v in credential["secret"].values() if isinstance(v, str) and v}, key=len, reverse=True)
    for _ in range(20):
        for value in values:
            text = text.replace(value, "[redacted]")
        decoded = re.sub(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])',
                         lambda match: json.loads('"' + match[0] + '"'), text)
        if decoded == text:
            break
        text = decoded
    else:
        raise ValueError("Upwork response exceeds the JSON escape nesting limit.")
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8").replace("\x00", "\\u0000")


def redact_json(data, credential: StoredCredential):
    """Redact decoded JSON values and keys within the provider result budget."""
    remaining = 20000

    def redact(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ValueError("Upwork response exceeds the nesting or item limit.")
        if isinstance(value, str):
            # Preserve ordinary JSON string content when it contains no secret.
            decoded = redact_text(value, credential)
            return decoded if "[redacted]" in decoded else value
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                safe_key = redact(key, depth + 1)
                if safe_key in result:
                    raise ValueError("Upwork response keys collide after credential redaction.")
                result[safe_key] = redact(child, depth + 1)
            return result
        if isinstance(value, list):
            return [redact(child, depth + 1) for child in value]
        return value

    return redact(data)


def response_object(text: str) -> JSONObject:
    """Decode one complete bounded provider result without parameter scanning."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Upwork response exceeds the 1 MiB transport limit; request a smaller page.")

    def pairs(rows):
        value = {}
        for key, child in rows:
            if key in value:
                raise ValueError("Duplicate Upwork response key.")
            value[key] = child
        return value

    try:
        data = json.loads(text, object_pairs_hook=pairs)
        json.dumps(data, allow_nan=False)
    except (ValueError, RecursionError):
        raise ValueError("Upwork returned invalid JSON data.") from None
    if not isinstance(data, dict) or not set(data) - {"trace_id", "status"}:
        raise ValueError("Upwork returned an unsupported JSON result shape.")
    return cast(JSONObject, data)


def structured_result(text: str, action: str, credential: StoredCredential) -> JSONObject:
    """Keep known fields typed and preserve unfamiliar data without clipping."""
    data = response_object(text)
    data = redact_json(data, credential)

    def project(value, schema, depth=0):
        kind = schema.get("type")
        if kind == "object" and isinstance(value, dict):
            result, complete = {}, True
            fields = schema["properties"]
            required = schema.get("required", [])
            missing = [key for key in required if key not in value]
            if missing:
                raise ValueError("Upwork response is missing required result fields: " + ", ".join(missing) + ".")
            for key, child in value.items():
                if depth == 0 and key == "trace_id":
                    continue  # Provider tracing metadata is not account/job data.
                if key not in fields:
                    complete = False
                    continue
                parsed, covered = project(child, fields[key], depth + 1)
                if parsed is not _MISSING:
                    if key in required and parsed == "":
                        raise ValueError("Upwork response has empty required result fields.")
                    result[key] = parsed
                elif key in required:
                    raise ValueError("Upwork response has invalid required result fields.")
                complete = complete and covered
            return result, complete
        if kind == "array" and isinstance(value, list):
            result, complete = [], True
            for child in value:
                parsed, covered = project(child, schema["items"], depth + 1)
                if parsed is _MISSING:
                    return _MISSING, False
                result.append(parsed)
                complete = complete and covered
            return result, complete
        if kind == "integer" and type(value) is float and value.is_integer():
            return int(value), True
        valid = ((kind == "string" and isinstance(value, str)) or
                 (kind == "boolean" and isinstance(value, bool)) or
                 (kind == "integer" and type(value) is int) or
                 (kind == "number" and type(value) in (int, float)) or
                 (kind == "null" and value is None))
        return (value, True) if valid else (_MISSING, False)

    projected, complete = project(data, BASE_SCHEMAS[action])
    result = cast(JSONObject, projected)
    required = cast(list[str], output_schema(action).get("required", []))
    if not complete or "provider_details_json" in required:
        result["provider_details_json"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if any(key not in result for key in required):
        raise ValueError("Upwork response is missing required result fields.")
    return result


_MISSING = object()
