# Action readiness and public policy vetoes

V5.5 readiness describes the authority of the recommended next action. A policy
rejection cannot retain an application-ready, allocated-work or captured-step
label. The assessment, final decision, evidence view and ordering key must agree.

| Current facts | Readiness | Next action |
| --- | --- | --- |
| Public policy blocker, such as an incompatible country, unsupported role or scam threshold | `blocked` | `skip` |
| Explicit public closure or elapsed captured deadline | `closed` | `skip` |
| Existing application awaiting a response | `watch` | `await_response` |
| Account restriction without a public suitability rejection | `watch` | Verify access or await a change |
| Eligible exact role with all application prerequisites supported | `application_ready` | `apply` |
| Currently supported task allocation and prerequisites | `allocated` | `start_allocated_task` |

Closed and blocked records have zero action-readiness weight. Their lifecycle,
public provenance, pay claims, qualifications and dated account/outcome history
remain available. Account waiting, exact-attempt closure and public role closure
are separate facts; this change does not close a different application or role.

The final decision boundary also honors a blocker added by the optional later
scam review. It replaces the earlier action and readiness view before returning
the rejected decision. The result is resorted and recounted after such changes,
so a newly rejected record cannot keep its earlier position ahead of ready work.
Tests use a mocked reviewer and require no LLM provider call.

Readiness changes alone do not establish new evidence, restore qualifications,
invent allocation or authorize an external action. Reassessment from corrected
source observations can restore a genuine application-ready action. Private
outcome events and aliases retain their normal scope and approval requirements.

The controls cover native public vetoes and closure, positive ready/allocated
paths, account waiting, retained assessment history, late risk refinement and
temporary-store restoration. Frozen comparisons also check the entire rejected
universe and preserve surfaced order; compact selected-pay deltas alone cannot
prove readiness consistency.
