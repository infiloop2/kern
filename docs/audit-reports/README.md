# Audit Reports

This folder holds recurring AI and human audits for Kern, one document per
audit axis. Each document is a collaborative, in-place record: the audit
question and review instructions stay stable, reviewers mark the commits they
examined, and every reviewer contributes to one canonical findings table.

| Axis | Document |
| --- | --- |
| Security: agent isolation from other user data | [01-security-agent-isolation.md](01-security-agent-isolation.md) |
| Security: network proxy policy enforcement | [02-security-network-policy.md](02-security-network-policy.md) |
| Security: Admin UI exposure of agent-controlled content | [03-security-admin-ui.md](03-security-admin-ui.md) |
| Security: public exposure of the admin UI and API | [04-security-admin-access.md](04-security-admin-access.md) |
| Security: installed apps and agent-authored content | [05-security-apps.md](05-security-apps.md) |
| Security: bundled tools, approvals, and data disclosure | [06-security-tools.md](06-security-tools.md) |
| Product UX: settings clarity, no surprises | [07-ux-settings-clarity.md](07-ux-settings-clarity.md) |
| Reliability: resource isolation and recovery | [08-reliability.md](08-reliability.md) |

## Document structure

Every axis has the same five durable parts:

1. **Audit question** states the guarantee in a few lines.
2. **Reviewed commits** is the top-level coverage indicator. One row means the
   named reviewer completed the whole axis at that exact commit; it does not
   mean every file in the repository was reviewed. It records only the current
   audit; a newer audit replaces the prior commit and reviewers.
3. **Findings** is the single source of truth. Audit reviewers append new
   finding rows and never edit an existing row.
4. **Audit instructions** are the threat model and minimal scope checklist.
   The checklist is a required baseline, not a complete definition of scope.
5. **Collaborative review** holds the shared evidence for each reviewed
   commit: methodology, concrete surface, coverage, confidence, and omissions.
   It is one report per commit, not one report per reviewer.

Git history preserves every earlier state, while the current files answer the
useful questions directly: which commits were reviewed, by whom, what remains
open, and where a fix was verified.

## How a sweep works

1. Check out a clean tree at a known commit. All review claims and line
   references are against that commit.
2. Pick one axis document. Read its audit question, threat model, and scope
   before reading any code. The threat model is binding: findings outside the
   stated scope belong in a different axis or are out of scope entirely.
3. Review the code independently. Reading the existing findings is encouraged,
   but first form your own view so earlier reviewers do not become the only
   search plan.
4. Append to the canonical **Findings** table:
   - Add a new stable `<PREFIX>-NNN` row for a materially new defect.
   - Never edit an existing row, including its severity, attribution,
     description, or resolution.
   - If your finding overlaps an existing one, append it only when you found a
     materially new trigger, impact, or affected surface. Describe only that
     new part instead of restating the earlier finding.
   - If you found nothing new, do not append a row.
   - Keep **Found at** as the earliest known affected commit. A later re-audit
     does not replace it.
   - Never delete or renumber a finding.
5. Contribute to the shared record for that commit under **Collaborative
   review**. Improve its methodology, reviewed-surface inventory, outcome, and
   coverage in place. Do not append a reviewer-owned report.
6. When the axis is fully covered, add yourself to that commit's **Reviewed
   commits** row. Multiple reviewers share one row. Stamp it even when you
   found nothing; the row records completed negative coverage. Partial work
   may improve the collaborative record and findings but does not earn a
   completed-review stamp.
7. A re-audit at a newer commit starts a fresh report. Preserve the audit
   question, findings table, threat model, and minimal scope checklist; clear
   the prior **Reviewed commits** coverage and **Collaborative review**, then
   record only the newer commit and its reviewers. Do not retain older commit
   subsections in the current document; Git history is their archive.

## Finding format

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| `<PREFIX>-NNN` | High | `<commit>` | Model or human | Concrete trigger, impact, affected code, and suggested fix when clear. | Open |

The description must contain a concrete failure scenario: what triggers the
defect and what goes wrong. A speculative concern without a scenario belongs
in review coverage or follow-up notes, not in the findings register.

Allowed resolutions are deliberately small:

| Resolution | Meaning |
| --- | --- |
| `Open` | The finding is still believed to apply and needs a decision or fix. |
| `Wontfix — <reason>` | The finding is accepted and intentionally will not be fixed. |
| `Fixed at <commit>` | A reviewer verified the fix at that exact commit. |

Audit reviewers set new findings to `Open`. Resolution changes are repository
owner actions, not audit-agent actions; an audit agent must not modify the
existing row.

## Collaborative review template

```markdown
## Collaborative review

### `<reviewed commit>`

Reviewed by: <all models and humans that completed this axis at this commit>

Methodology: <shared summary of static reading / grep sweeps / system runs /
tests / PoCs>

#### What was reviewed

Enumerate concrete files, entry points, config paths, listeners, helpers, and
tests. Be specific enough that a later reviewer can tell what the review does
and does not vouch for.

#### Coverage and confidence

Account for the axis's minimal scope checklist, what was deliberately skipped
and why, and where confidence is low. "I reviewed everything carefully" is
not acceptable.
```

## Severity scale

| Severity | Meaning | Anchor examples |
| --- | --- | --- |
| Critical | The axis's core guarantee is broken and exploitable in a default or documented configuration. | Agent reads another Unix user's secrets; proxy passes traffic to a host the policy denies; admin UI lets a third-party page act with the operator's credentials. |
| High | Core guarantee broken, but only under an unusual-yet-plausible configuration, race, or precondition. | Policy bypass requiring a specific rule shape an operator could reasonably write; UI setting whose actual effect contradicts its label. |
| Medium | A real weakness with bounded impact, one that needs a second bug or unlikely precondition to break the guarantee, or a reliability failure that a host reboot fully resolves. | A missing validation the next layer currently catches; an operator-control outage cleared by reboot; unbounded growth that takes months to matter. |
| Low | Hardening gap or deviation from stated design with no identified path to breaking the guarantee. | Overly broad file mode on a non-secret; confusing but technically accurate UI copy. |
| Info | Observation worth recording; not a defect. | Documentation drift; suggested test coverage. |

## Ground rules

- The findings table is authoritative. The **Collaborative review** may refer
  to finding IDs but must not contain a competing findings table.
- A reviewed-commit row is a strong claim: add it only after covering the
  whole audit question or explicitly recording each skipped checklist item.
- Report what the code does, not what documentation promises. Code/doc drift
  can itself be a finding.
- Do not pad. Five verified findings beat twenty speculative ones; a clean
  sweep with rigorous coverage is useful.
- Entries must be understandable without a session transcript.
